import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from gamdl.app import AppPaths, DownloadJob, DownloadService, JobCancelledError
from gamdl.downloader.downloader import AppleMusicDownloader
from gamdl.downloader.downloader_base import AppleMusicBaseDownloader
from gamdl.downloader.types import DownloadItem, UrlInfo


class StreamingPreparationTests(unittest.TestCase):
    def make_downloader(self, root, count=12):
        tracks=[{'id':str(i), 'type':'songs', 'attributes':{'name':f'Song {i}',
                 'playParams':{'id':str(i)}, 'url':f'https://music.apple.com/cn/song/test/{i}'}}
                for i in range(1,count+1)]
        collection={'type':'playlists','attributes':{'name':'Test','playParams':{'id':'pl.test'}},
                    'relationships':{'tracks':{'data':tracks}}}
        async def pages(_):
            if False: yield None
        api=SimpleNamespace(storefront='cn',get_playlist=AsyncMock(return_value={'data':[collection]}),
                            extend_api_data=pages)
        downloader=AppleMusicDownloader.__new__(AppleMusicDownloader)
        downloader.interface=SimpleNamespace(apple_music_api=api)
        base=AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.base_downloader=base
        downloader.flat_filter=None
        async def prepare(metadata,playlist_metadata=None,**kwargs):
            tags=kwargs.get('playlist_tags_override')
            return DownloadItem(media_metadata=metadata,playlist_metadata=playlist_metadata,
                                playlist_tags=tags,final_path=str(root/f'[{tags.playlist_track:02}]Song.m4a'))
        downloader.song_downloader=SimpleNamespace(get_download_item=prepare)
        downloader.get_url_info=lambda _:UrlInfo(type='playlist',id='pl.test')
        return downloader,collection

    def test_download_starts_before_slow_song_is_ready(self):
        async def scenario(root):
            downloader,_=self.make_downloader(root)
            start=asyncio.Event(); original=downloader.song_downloader.get_download_item
            slow_finished=False; starts=[]; snapshots=[]
            async def prepare(metadata,*args,**kwargs):
                nonlocal slow_finished
                if metadata['id']=='1':
                    await start.wait()
                    slow_finished=True
                return await original(metadata,*args,**kwargs)
            async def download(item):
                starts.append((item.playlist_tags.playlist_track,slow_finished))
                start.set()
                await asyncio.sleep(.005)
            downloader.song_downloader.get_download_item=prepare
            downloader.download=download
            service=DownloadService('test',paths=AppPaths(base_dir=root/'app'),progress_callback=snapshots.append)
            with patch.object(service,'_create_downloader',new=AsyncMock(return_value=downloader)):
                result=await asyncio.wait_for(service.run(DownloadJob(['test'],str(root),download_concurrency=2)),timeout=3)
            self.assertNotEqual(starts[0][0],1)
            self.assertFalse(starts[0][1])
            self.assertEqual(sorted(i for i,_ in starts),list(range(1,13)))
            self.assertEqual((result.prepared_items,result.preparation_total,result.downloaded_items),(12,12,12))
            self.assertTrue(any(s['active_downloads']>0 and s['prepared_items']<s['preparation_total'] for s in snapshots))
            self.assertLessEqual(max(s['active_downloads'] for s in snapshots),2)
        with tempfile.TemporaryDirectory() as folder: asyncio.run(scenario(Path(folder)))

    def test_duplicate_songs_keep_distinct_playlist_positions(self):
        async def scenario(root):
            downloader,collection=self.make_downloader(root,2)
            collection['relationships']['tracks']['data'][1]=collection['relationships']['tracks']['data'][0].copy()
            values=[value async for value in downloader.iter_prepared_download_items(UrlInfo(type='playlist',id='pl.test'))]
            self.assertEqual(values[0][:2],(0,2))
            items=sorted((index,item) for index,_,item in values if item)
            self.assertEqual([item.playlist_tags.playlist_track for _,item in items],[1,2])
            self.assertNotEqual(items[0][1].final_path,items[1][1].final_path)
        with tempfile.TemporaryDirectory() as folder: asyncio.run(scenario(Path(folder)))

    def test_failed_preparation_does_not_block_other_downloads(self):
        async def scenario(root):
            downloader,_=self.make_downloader(root,5)
            original=downloader.song_downloader.get_download_item
            async def prepare(metadata,*args,**kwargs):
                if metadata['id']=='3':raise RuntimeError('metadata unavailable')
                return await original(metadata,*args,**kwargs)
            downloader.song_downloader.get_download_item=prepare
            downloader.download=AsyncMock()
            service=DownloadService('test',paths=AppPaths(base_dir=root/'app'))
            with patch.object(service,'_create_downloader',new=AsyncMock(return_value=downloader)):
                result=await asyncio.wait_for(service.run(DownloadJob(['test'],str(root))),timeout=3)
            self.assertEqual((result.prepared_items,result.downloaded_items,result.errors),(5,4,1))
            self.assertEqual(result.failed_items[0]['retry_target']['playlist_tags']['playlist_track'],3)
        with tempfile.TemporaryDirectory() as folder:asyncio.run(scenario(Path(folder)))

    def test_cancel_drains_preparation_and_stops_new_downloads(self):
        async def scenario(root):
            downloader,_=self.make_downloader(root,40)
            cancelled=False; starts=[]
            async def download(item):
                nonlocal cancelled
                starts.append(item);cancelled=True
                await asyncio.sleep(.01)
            downloader.download=download
            service=DownloadService('test',paths=AppPaths(base_dir=root/'app'),cancel_callback=lambda:cancelled)
            with patch.object(service,'_create_downloader',new=AsyncMock(return_value=downloader)):
                with self.assertRaises(JobCancelledError) as caught:
                    await asyncio.wait_for(service.run(DownloadJob(['test'],str(root))),timeout=3)
            self.assertEqual(len(starts),1)
            self.assertEqual(caught.exception.result['active_downloads'],0)
        with tempfile.TemporaryDirectory() as folder:asyncio.run(scenario(Path(folder)))

    def test_closing_stream_cancels_preparation_workers(self):
        async def scenario(root):
            downloader,_=self.make_downloader(root,100)
            stream=downloader.iter_prepared_download_items(UrlInfo(type='playlist',id='pl.test'),preparation_concurrency=2)
            await stream.__anext__();await stream.__anext__()
            await asyncio.wait_for(stream.aclose(),timeout=1)
            self.assertEqual(len([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]),0)
        with tempfile.TemporaryDirectory() as folder:asyncio.run(scenario(Path(folder)))

    def test_api_failure_finishes_workers_without_hanging(self):
        async def scenario(root):
            downloader,_=self.make_downloader(root)
            downloader.interface.apple_music_api.get_playlist=AsyncMock(side_effect=RuntimeError('fetch failed'))
            service=DownloadService('test',paths=AppPaths(base_dir=root/'app'))
            with patch.object(service,'_create_downloader',new=AsyncMock(return_value=downloader)):
                result=await asyncio.wait_for(service.run(DownloadJob(['test'],str(root))),timeout=2)
            self.assertEqual((result.downloaded_items,result.errors),(0,1))
        with tempfile.TemporaryDirectory() as folder:asyncio.run(scenario(Path(folder)))

    def test_album_pagination_and_preparation_limit(self):
        async def scenario(root):
            downloader,collection=self.make_downloader(root,5)
            collection['type']='albums'
            extra=collection['relationships']['tracks']['data'].pop()
            api=downloader.interface.apple_music_api
            api.get_album=AsyncMock(return_value={'data':[collection]})
            async def pages(_): yield {'data':[extra]}
            api.extend_api_data=pages
            active=0;peak=0;barrier=asyncio.Event()
            async def prepare(metadata,playlist_metadata=None,**kwargs):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                if active==2:barrier.set()
                await barrier.wait();await asyncio.sleep(.005)
                active-=1
                return DownloadItem(media_metadata=metadata)
            downloader.song_downloader.get_download_item=prepare
            values=[value async for value in downloader.iter_prepared_download_items(
                UrlInfo(type='album',id='1'),preparation_concurrency=2)]
            self.assertEqual(values[0][:2],(0,5))
            self.assertEqual(sorted(i for i,_,item in values if item),[1,2,3,4,5])
            self.assertEqual(peak,2)
        with tempfile.TemporaryDirectory() as folder:asyncio.run(scenario(Path(folder)))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for audio integration tests")
    def test_real_conversion_finishes_while_later_song_is_still_preparing(self):
        import shutil,subprocess
        from gamdl.app.executables import ExecutableResolution
        ffmpeg=shutil.which('ffmpeg')
        async def scenario(root):
            sample=root/'sample.m4a'
            subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','sine=duration=0.1','-c:a','aac',str(sample)],check=True)
            downloader,_=self.make_downloader(root,5)
            converted_first=asyncio.Event(); original=downloader.song_downloader.get_download_item
            async def prepare(metadata,*args,**kwargs):
                if metadata['id']=='5':await converted_first.wait()
                return await original(metadata,*args,**kwargs)
            async def download(item):shutil.copyfile(sample,item.final_path)
            snapshots=[]
            def progress(result):
                snapshots.append(result)
                if result['converted_items']>0:converted_first.set()
            downloader.song_downloader.get_download_item=prepare;downloader.download=download
            service=DownloadService('test',paths=AppPaths(base_dir=root/'app'),progress_callback=progress)
            with patch.object(service,'_create_downloader',new=AsyncMock(return_value=downloader)), \
                 patch('gamdl.app.conversion.resolve_executable',return_value=ExecutableResolution('ffmpeg',ffmpeg,'bundled')):
                result=await asyncio.wait_for(service.run(DownloadJob(['test'],str(root),download_output_format='flac',
                    download_concurrency=3,conversion_concurrency=2)),timeout=5)
            self.assertEqual((result.prepared_items,result.downloaded_items,result.converted_items,result.errors),(5,5,5,0))
            self.assertTrue(any(s['converted_items']>0 and s['prepared_items']<5 for s in snapshots))
            self.assertEqual(len(list(root.glob('*.flac'))),5)
        with tempfile.TemporaryDirectory() as folder:asyncio.run(scenario(Path(folder)))
