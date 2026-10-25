import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from gamdl.app import AppPaths, AppSettingsStore, DownloadJob, DownloadService, JobCancelledError
from gamdl.app.conversion import ConversionService, ConversionJobSpec
from gamdl.app.executables import ExecutableResolution
from gamdl.downloader.types import DownloadItem
from gamdl.web_gui import Job, JobManager

FFMPEG = shutil.which('ffmpeg')


class PipelineTests(unittest.TestCase):
    @unittest.skipUnless(FFMPEG, "ffmpeg is required for audio integration tests")
    def test_download_and_conversion_overlap_with_eight_workers_and_real_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sample = root / 'sample.m4a'
            subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=frequency=1000:duration=0.2', '-c:a', 'aac', str(sample)], check=True)
            items = [DownloadItem(media_metadata={'id': str(i), 'type': 'songs',
                                  'attributes': {'name': f'Song {i}'}},
                                  final_path=str(root / 'out' / f'[{i:02}]Song.m4a')) for i in range(1, 17)]
            active = 0
            peak = 0
            first_wave = asyncio.Event()
            class Downloader:
                interface = SimpleNamespace(apple_music_api=SimpleNamespace(storefront='cn'))
                def get_url_info(self, _): return SimpleNamespace(type='playlist')
                async def get_download_queue(self, _): return items
                async def download(self, item):
                    nonlocal active, peak
                    active += 1
                    peak = max(peak, active)
                    if active == 8: first_wave.set()
                    await first_wave.wait()
                    await asyncio.sleep(.02)
                    Path(item.final_path).parent.mkdir(exist_ok=True)
                    shutil.copyfile(sample, item.final_path)
                    active -= 1
            snapshots = []
            service = DownloadService('test', paths=AppPaths(base_dir=root / 'app'),
                                      progress_callback=snapshots.append)
            barrier = threading.Barrier(8)
            converting = 0
            conversion_peak = 0
            calls = 0
            lock = threading.Lock()
            original_run = ConversionService.run
            def real_convert(converter, spec):
                nonlocal converting, conversion_peak, calls
                with lock:
                    calls += 1
                    first_batch = calls <= 8
                    converting += 1
                    conversion_peak = max(conversion_peak, converting)
                try:
                    if first_batch: barrier.wait(timeout=5)
                    return original_run(converter, spec)
                finally:
                    with lock: converting -= 1
            with patch.object(service, '_create_downloader', new=AsyncMock(return_value=Downloader())), \
                 patch('gamdl.app.conversion.resolve_executable', return_value=ExecutableResolution('ffmpeg', FFMPEG, 'bundled')), \
                 patch.object(ConversionService, 'run', real_convert):
                result = asyncio.run(service.run(DownloadJob(['test'], str(root / 'out'), download_output_format='flac')))
            self.assertEqual(peak, 8)
            self.assertEqual(conversion_peak, 8)
            self.assertTrue(any(s['active_downloads'] > 0 and s['active_conversions'] > 0 for s in snapshots))
            self.assertEqual((result.downloaded_items, result.converted_items, result.errors), (16, 16, 0))
            self.assertEqual((result.active_downloads, result.active_conversions), (0, 0))
            self.assertEqual(len(list((root / 'out').glob('*.flac'))), 16)
            self.assertEqual(list((root / 'out').glob('*.m4a')), [])
            self.assertEqual(list((root / 'out').glob('.amd-convert-*')), [])
            for path in (root / 'out').glob('*.flac'):
                subprocess.run([FFMPEG, '-v', 'error', '-i', str(path), '-f', 'null', '-'], check=True)

    def test_failed_conversion_preserves_existing_destination_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.m4a'; source.write_bytes(b'original audio')
            destination = root / 'input.flac'; destination.write_bytes(b'existing output')
            converter = ConversionService()
            converter.full_ffmpeg_path = FFMPEG or "ffmpeg"
            with patch.object(converter, '_read_metadata', return_value=SimpleNamespace(sample_rate=44100)), \
                 patch.object(converter, '_run_ffmpeg', side_effect=RuntimeError('encoding failed')):
                result = converter.run(ConversionJobSpec('file', str(source), str(root), 'flac', True))
            self.assertEqual(result.errors, 1)
            self.assertEqual(source.read_bytes(), b'original audio')
            self.assertEqual(destination.read_bytes(), b'existing output')
            self.assertEqual(list(root.glob('.amd-convert-*')), [])

    def test_cancellation_finishes_current_downloads_and_does_not_start_more(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); cancel = threading.Event(); started = []
            items = [DownloadItem(media_metadata={'id': str(i), 'type': 'songs', 'attributes': {'name': str(i)}},
                                  final_path=str(root / f'{i}.m4a')) for i in range(16)]
            class Downloader:
                interface = SimpleNamespace(apple_music_api=SimpleNamespace(storefront='cn'))
                def get_url_info(self, _): return SimpleNamespace(type='playlist')
                async def get_download_queue(self, _): return items
                async def download(self, item):
                    started.append(item)
                    if len(started) == 8: cancel.set()
                    await asyncio.sleep(.01)
            service = DownloadService('test', paths=AppPaths(base_dir=root / 'app'), cancel_callback=cancel.is_set)
            with patch.object(service, '_create_downloader', new=AsyncMock(return_value=Downloader())):
                with self.assertRaises(JobCancelledError) as raised:
                    asyncio.run(service.run(DownloadJob(['test'], str(root))))
            self.assertEqual(len(started), 8)
            self.assertEqual(raised.exception.result['active_downloads'], 0)

    def test_independent_conversion_queue_runs_while_download_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); paths = AppPaths(base_dir=root)
            started = threading.Event(); converted = threading.Event(); release = threading.Event()
            def block_download(manager, job):
                started.set(); release.wait(timeout=3)
            def convert(manager, job): converted.set()
            with patch.object(JobManager, '_run_download_job', block_download), \
                 patch.object(JobManager, '_run_conversion_job', convert):
                manager = JobManager(SimpleNamespace(), AppSettingsStore(paths), paths)
                download = Job('d', [], [], {}, [], kind='download')
                conversion = Job('c', [], [], {}, [], kind='convert')
                manager.jobs.update(d=download, c=conversion)
                try:
                    manager.job_queue.put('d')
                    self.assertTrue(started.wait(timeout=1))
                    manager.conversion_queue.put('c')
                    self.assertTrue(converted.wait(timeout=1))
                    self.assertFalse(release.is_set())
                finally:
                    release.set(); manager.shutdown()

    def test_task_log_retains_early_entries_after_5000_lines(self):
        job = Job('test', [], [], {}, [])
        for i in range(5000): job.append_log(f'line {i}')
        self.assertEqual(len(job.logs), 5000)
        self.assertEqual(job.logs[0], 'line 0')

    def test_format_setting_persists_and_rejects_unknown_format(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AppSettingsStore(AppPaths(base_dir=Path(folder)))
            self.assertEqual(store.defaults().download_output_format, 'original')
            for value in ['flac', 'mp3', 'original']:
                store.save({'download_output_format': value, 'output_path': str(Path(folder) / 'out')})
                self.assertEqual(store.load().download_output_format, value)
            with self.assertRaises(ValueError):
                store.save({'download_output_format': 'wav'})

    @unittest.skipUnless(FFMPEG, "ffmpeg is required for audio integration tests")
    def test_mp3_pipeline_keeps_preexisting_source_and_skips_existing_target(self):
        from gamdl.downloader.exceptions import MediaFileExists
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'existing.m4a'
            subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=duration=0.2', '-c:a', 'aac', str(source)], check=True)
            original = source.read_bytes()
            item = DownloadItem(media_metadata={'id':'1', 'type':'songs', 'attributes':{'name':'Song'}}, final_path=str(source))
            class Downloader:
                interface = SimpleNamespace(apple_music_api=SimpleNamespace(storefront='cn'))
                def get_url_info(self, _): return SimpleNamespace(type='song')
                async def get_download_queue(self, _): return [item]
                async def download(self, _): raise MediaFileExists(str(source))
            service = DownloadService('test', paths=AppPaths(base_dir=root / 'app'))
            with patch.object(service, '_create_downloader', new=AsyncMock(return_value=Downloader())), \
                 patch('gamdl.app.conversion.resolve_executable', return_value=ExecutableResolution('ffmpeg', FFMPEG, 'bundled')):
                result = asyncio.run(service.run(DownloadJob(['test'], str(root), download_output_format='mp3')))
                self.assertEqual((result.converted_items, result.errors), (1, 0))
                self.assertEqual(source.read_bytes(), original)
                output = source.with_suffix('.mp3')
                self.assertTrue(output.exists())
                subprocess.run([FFMPEG, '-v', 'error', '-i', str(output), '-f', 'null', '-'], check=True)
                again = asyncio.run(service.run(DownloadJob(['test'], str(root), download_output_format='mp3')))
                self.assertEqual((again.downloaded_items, again.conversion_skipped, again.errors), (0, 1, 0))

    @unittest.skipUnless(FFMPEG, "ffmpeg is required for audio integration tests")
    def test_in_place_conversion_uses_temporary_output_and_keeps_valid_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'same.flac'
            subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=duration=0.1', '-c:a', 'flac', str(source)], check=True)
            with patch('gamdl.app.conversion.resolve_executable', return_value=ExecutableResolution('ffmpeg', FFMPEG, 'bundled')):
                converter = ConversionService()
                result = converter.run(ConversionJobSpec('file', str(source), str(source.parent), 'flac', True))
            self.assertEqual((result.converted_files, result.errors), (1, 0))
            subprocess.run([FFMPEG, '-v', 'error', '-i', str(source), '-f', 'null', '-'], check=True)

    @unittest.skipUnless(FFMPEG, "ffmpeg is required for audio integration tests")
    def test_configured_limits_and_keep_original_files_are_applied(self):
        for keep in (False, True):
            with self.subTest(keep_original_files=keep), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                sample = root / 'sample.m4a'
                subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                                'sine=duration=0.1', '-c:a', 'aac', str(sample)], check=True)
                out = root / 'out'; out.mkdir()
                items = [DownloadItem(media_metadata={'id':str(i), 'type':'songs', 'attributes':{'name':str(i)}},
                                      final_path=str(out / f'{i}.m4a')) for i in range(7)]
                class Downloader:
                    interface = SimpleNamespace(apple_music_api=SimpleNamespace(storefront='cn'))
                    def get_url_info(self, _): return SimpleNamespace(type='playlist')
                    async def get_download_queue(self, _): return items
                    async def download(self, item):
                        await asyncio.sleep(.02)
                        shutil.copyfile(sample, item.final_path)
                snapshots=[]
                service=DownloadService('test', paths=AppPaths(base_dir=root / 'app'), progress_callback=snapshots.append)
                barrier=threading.Barrier(2); lock=threading.Lock(); calls=0
                original_run=ConversionService.run
                def convert(converter, job):
                    nonlocal calls
                    with lock:
                        calls+=1; first=calls <= 2
                    if first: barrier.wait(timeout=3)
                    return original_run(converter, job)
                with patch.object(service, '_create_downloader', new=AsyncMock(return_value=Downloader())), \
                     patch('gamdl.app.conversion.resolve_executable', return_value=ExecutableResolution('ffmpeg', FFMPEG, 'bundled')), \
                     patch.object(ConversionService, 'run', convert):
                    result=asyncio.run(service.run(DownloadJob(['test'], str(out), download_output_format='flac',
                        download_concurrency=3, conversion_concurrency=2, keep_original_files=keep)))
                self.assertEqual(max(s['active_downloads'] for s in snapshots),3)
                self.assertEqual(max(s['active_conversions'] for s in snapshots),2)
                self.assertEqual((result.download_concurrency,result.conversion_concurrency),(3,2))
                self.assertEqual((result.converted_items,result.errors),(7,0))
                self.assertEqual(len(list(out.glob('*.m4a'))),7 if keep else 0)
                self.assertEqual(len(list(out.glob('*.flac'))),7)

    def test_concurrency_settings_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            store=AppSettingsStore(AppPaths(base_dir=Path(folder)))
            saved=store.save({'output_path':str(Path(folder)/'out'), 'download_concurrency':3,
                             'conversion_concurrency':2, 'keep_original_files':True})
            loaded=store.load()
            self.assertEqual((loaded.download_concurrency,loaded.conversion_concurrency,loaded.keep_original_files),(3,2,True))
            for key in ['download_concurrency','conversion_concurrency']:
                for invalid in [0,33,True,1.5,'8',None]:
                    with self.subTest(key=key,value=invalid), self.assertRaises(ValueError):
                        store.save({key:invalid})

    def test_standalone_conversion_uses_configured_parallelism(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); inputs=root/'in'; inputs.mkdir()
            for i in range(9): (inputs/f'{i}.wav').touch()
            converter=ConversionService(concurrency=3)
            converter.full_ffmpeg_path=FFMPEG or "ffmpeg"
            lock=threading.Lock(); barrier=threading.Barrier(3)
            active=0; peak=0; calls=0
            def convert(source,destination,target,overwrite,result):
                nonlocal active,peak,calls
                with lock:
                    calls+=1; first=calls<=3; active+=1; peak=max(peak,active)
                if first: barrier.wait(timeout=3)
                time.sleep(.01)
                with lock: active-=1
                result.converted_files+=1
            with patch.object(converter,'_convert_path',convert):
                result=converter.run(ConversionJobSpec('directory',str(inputs),str(root/'out'),'flac'))
            self.assertEqual((peak,result.converted_files,result.conversion_concurrency),(3,9,3))
