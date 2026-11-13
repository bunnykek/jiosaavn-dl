"""Offline regression tests. The fixture is 0.1 seconds of generated silence."""
import contextlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

import requests
from mutagen.mp4 import MP4

import jiosaavn as app

FIXTURE = Path(__file__).parent / 'fixtures' / 'silence.m4a'
URL = 'https://www.jiosaavn.com/song/example/token'
SONG = {
    'song': 'A: Song &amp; More', 'album': 'An Album', 'year': '2024',
    'primary_artists': 'Artist', 'encrypted_media_url': 'encrypted',
    'id': '123', 'has_lyrics': 'false', 'explicit_content': '0',
}


def response(content=b'', text='', headers=None):
    result = Mock()
    result.text = text
    result.content = content
    result.headers = headers or {}
    result.iter_content.return_value = [content]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class UrlTests(unittest.TestCase):
    def test_supported_links(self):
        for path, expected in (
            ('song/name/token', 'song'), ('album/name/token', 'album'),
            ('featured/name/token', 'playlist'), ('s/playlist/user/name/token', 'playlist'),
            ('playlist/name/token', 'playlist'), ('s/album/hindi/name/token', 'album'),
            ('s/song/hindi/name/token', 'song'),
            ('p/album/hindi/name/token', 'album'), ('p/song/hindi/name/token', 'song'),
            ('p/playlist/user/name/token', 'playlist'),
            ('s/featured/hindi/name/token', 'playlist'), ('p/featured/hindi/name/token', 'playlist'),
        ):
            with self.subTest(path=path):
                self.assertEqual(app.parse_url(f'https://www.jiosaavn.com/{path}/?source=share#x'),
                                 (expected, 'token'))

    def test_legacy_saavn_featured_link_with_underscore_token(self):
        self.assertEqual(
            app.parse_url('https://www.saavn.com/s/featured/hindi/apne-desi-gaane-carvaan-select/'
                          'r7OrmfAcC4JTHgJw1pi47A__'),
            ('playlist', 'r7OrmfAcC4JTHgJw1pi47A__'))

    def test_mobile_legacy_and_schemeless_links(self):
        for host in app.SITE_HOSTS:
            for scheme in ('https://', 'http://', ''):
                with self.subTest(host=host, scheme=scheme):
                    self.assertEqual(app.parse_url(f'{scheme}{host}/song/name/token'), ('song', 'token'))

    def test_html_escaped_shared_url(self):
        self.assertEqual(app.parse_url(URL + '?x=1&amp;y=2'), ('song', 'token'))

    def test_short_link_resolution(self):
        client = Mock()
        redirect = response(headers={'Location': URL})
        redirect.status_code = 302
        client.request.return_value = redirect
        self.assertEqual(app.resolve_url('https://saavn.app.link/example', client), ('song', 'token'))
        self.assertFalse(client.request.call_args.kwargs['allow_redirects'])

    def test_short_link_rejects_external_destination(self):
        client = Mock()
        redirect = response(headers={'Location': 'https://evil.com/song/name/token'})
        redirect.status_code = 302
        client.request.return_value = redirect
        with self.assertRaises(app.DownloadError):
            app.resolve_url('https://saavn.app.link/example', client)
        self.assertEqual(client.request.call_count, 1)

    def test_short_link_loop_is_bounded(self):
        client = Mock()
        redirect = response(headers={'Location': '/loop'})
        redirect.status_code = 302
        client.request.return_value = redirect
        with self.assertRaises(app.DownloadError):
            app.resolve_url('https://saavn.app.link/example', client)
        self.assertEqual(client.request.call_count, 6)

    def test_short_link_without_web_redirect(self):
        client = Mock()
        result = response()
        result.status_code = 200
        client.request.return_value = result
        with self.assertRaisesRegex(app.DownloadError, 'full website URL'):
            app.resolve_url('https://saavn.app.link/example', client)

    def test_invalid_links(self):
        for url in ('https://evil.com/song/name/id', 'file:///song/name/id',
                    'https://www.jiosaavn.com/artist/name/id',
                    'https://www.jiosaavn.com/song/name/%2Ftmp', 'not-a-url'):
            with self.subTest(url=url), self.assertRaises(app.DownloadError):
                app.parse_url(url)

    def test_portable_names(self):
        for name in ('../../escape', 'AC/DC', 'A:B?C', '.', '', 'CON', 'x' * 300):
            result = app.safe_name(name)
            self.assertTrue(result)
            self.assertNotIn('/', result)
            self.assertNotIn('\\', result)
            self.assertLessEqual(len(result), 160)
            self.assertNotIn(result, ('.', '..', 'CON'))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Mock()
        self.client = app.Jiosaavn(self.temp.name, session=self.session, cover=False)
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        self.addCleanup(self.stack.close)

    def prepare_track(self):
        self.client.metadata = Mock(return_value={'123': dict(SONG)})
        self.client.getCdnURL = Mock(return_value='https://aac.example/song.m4a')
        self.session.get.return_value = response(FIXTURE.read_bytes())

    def test_json_and_legacy_callback(self):
        for text in ('{"title": "Album"}', 'callback({"title": "Album"});'):
            self.session.get.return_value = response(text=text)
            self.assertEqual(self.client.metadata('album', 'id')['title'], 'Album')
        self.assertEqual(self.session.get.call_args.kwargs['timeout'], 30)

    def test_invalid_api_responses(self):
        for text in ('<html>Unavailable</html>', '{}', '[]', '{"error":"unavailable"}'):
            self.session.get.return_value = response(text=text)
            with self.subTest(text=text), self.assertRaises(app.DownloadError):
                self.client.metadata('song', 'id')

    def test_http_errors(self):
        self.session.get.return_value.raise_for_status.side_effect = requests.HTTPError('403')
        with self.assertRaises(app.DownloadError):
            self.client.metadata('song', 'id')

    def test_real_download_and_tags(self):
        self.prepare_track()
        self.client.processTrack('token')
        path = next(Path(self.temp.name).rglob('*.m4a'))
        self.assertEqual(path.parent.name, 'An Album - Artist [2024]')
        tags = MP4(path)
        self.assertEqual(tags['\xa9nam'], ['A: Song & More'])
        self.assertEqual(tags['trkn'], [(1, 1)])
        self.assertEqual(tags['rtng'], [2])
        self.assertEqual(tags['----:com.apple.iTunes:JioSaavn ID'], [b'123'])
        self.assertEqual(self.client.downloaded, 1)
        self.assertFalse(list(Path(self.temp.name).rglob('*.part')))

    def test_skip_valid_file(self):
        self.prepare_track()
        self.client.processTrack('token')
        self.client.processTrack('token')
        self.assertEqual(self.client.skipped, 1)
        self.assertEqual(self.session.get.call_count, 1)

    def test_skip_tagged_track_after_position_changes(self):
        self.prepare_track()
        self.client.processTrack('token', song_pos=1)
        self.client.processTrack('token', song_pos=2)
        self.assertEqual(self.client.skipped, 1)
        self.assertEqual(self.session.get.call_count, 1)

    def test_dry_run_lists_track_without_media_requests_or_files(self):
        self.prepare_track()
        self.client.dry_run = True
        self.client.processTrack('token')
        self.assertEqual(self.client.planned, 1)
        self.assertEqual(self.client.downloaded, 0)
        self.client.getCdnURL.assert_not_called()
        self.session.get.assert_not_called()
        self.assertFalse(list(Path(self.temp.name).rglob('*')))
        self.assertIn('Would download:', self.output.getvalue())

    def test_quiet_dry_run_suppresses_routine_output(self):
        self.prepare_track()
        self.client.dry_run = True
        self.client.quiet = True
        self.client.processTrack('token')
        self.assertEqual(self.output.getvalue(), '')

    def test_verbose_dry_run_shows_song_id(self):
        self.prepare_track()
        self.client.dry_run = True
        self.client.verbose = True
        self.client.processTrack('token')
        self.assertIn('JioSaavn ID: 123', self.output.getvalue())

    def test_verbose_download_shows_song_id(self):
        self.prepare_track()
        self.client.verbose = True
        self.client.processTrack('token')
        self.assertIn('JioSaavn ID: 123', self.output.getvalue())

    def test_collection_track_metadata_avoids_song_lookup(self):
        self.prepare_track()
        collection_song = dict(SONG, perma_url=URL)
        self.client.metadata.return_value = {
            'title': 'My Album',
            'songs': [collection_song],
        }
        self.client.process_collection('album', 'collection-token')
        self.assertEqual(self.client.metadata.call_count, 1)
        self.assertEqual(self.client.downloaded, 1)

    def test_many_artists_are_not_guessed_to_be_various_artists(self):
        self.prepare_track()
        song = dict(SONG, primary_artists='Artist One, Artist Two, Artist Three')
        self.client.processTrack('token', song_data=song)
        path = next(Path(self.temp.name).rglob('*.m4a'))
        self.assertEqual(path.parent.name,
                         'An Album - Artist One, Artist Two, Artist Three [2024]')

    def test_current_song_list_response(self):
        self.prepare_track()
        self.client.metadata.return_value = {'songs': [dict(SONG)], 'modules': {}}
        self.client.processTrack('token')
        self.assertEqual(self.client.downloaded, 1)

    def test_direct_song_response(self):
        self.prepare_track()
        self.client.metadata.return_value = dict(SONG)
        self.client.processTrack('token')
        self.assertEqual(self.client.downloaded, 1)

    def test_replace_invalid_existing_file(self):
        self.prepare_track()
        self.client.processTrack('token')
        path = next(Path(self.temp.name).rglob('*.m4a'))
        path.write_bytes(b'error page')
        self.client.processTrack('token')
        self.assertEqual(self.client.downloaded, 2)
        self.assertEqual(MP4(path)['\xa9nam'], ['A: Song & More'])

    def test_tag_failure_preserves_old_file_and_cleans_temp(self):
        self.prepare_track()
        self.client.processTrack('token')
        path = next(Path(self.temp.name).rglob('*.m4a'))
        original = path.read_bytes()
        self.client.overwrite = True
        self.client.tagger = Mock(side_effect=ValueError('bad metadata'))
        with self.assertRaises(ValueError):
            self.client.processTrack('token')
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(Path(self.temp.name).rglob('*.part')))

    def test_truncated_and_empty_downloads(self):
        for content, headers in ((b'', {}), (b'error', {'Content-Type': 'text/html'})):
            self.session.get.return_value = response(content, headers=headers)
            with self.subTest(content=content), self.assertRaises(app.DownloadError):
                self.client.download_file('https://example.com/audio', Path(self.temp.name) / 'track.m4a')
            self.assertFalse(list(Path(self.temp.name).iterdir()))
        path = Path(self.temp.name) / 'track.m4a'
        self.session.get.return_value = response(b'a', headers={'Content-Length': '5'})
        with self.assertRaises(app.DownloadError):
            self.client.download_file('https://example.com/audio', path)
        self.assertEqual(path.with_name(path.name + '.part').read_bytes(), b'a')

    def test_interrupted_stream_preserves_partial_file(self):
        self.session.get.return_value = response()

        def interrupted():
            yield b'partial'
            raise requests.ConnectionError('lost connection')

        self.session.get.return_value.iter_content.return_value = interrupted()
        with self.assertRaises(app.DownloadError):
            self.client.download_file('https://example.com/audio', Path(self.temp.name) / 'track.m4a')
        self.assertEqual(
            (Path(self.temp.name) / 'track.m4a.part').read_bytes(), b'partial')

    def test_keyboard_interrupt_preserves_partial_file(self):
        self.session.get.return_value = response()

        def interrupted():
            yield b'partial'
            raise KeyboardInterrupt

        self.session.get.return_value.iter_content.return_value = interrupted()
        with self.assertRaises(KeyboardInterrupt):
            self.client.download_file('https://example.com/audio', Path(self.temp.name) / 'track.m4a')
        self.assertEqual(
            (Path(self.temp.name) / 'track.m4a.part').read_bytes(), b'partial')

    def test_resume_partial_download_with_range_request(self):
        path = Path(self.temp.name) / 'track.m4a'
        path.with_name(path.name + '.part').write_bytes(b'abc')
        result = response(b'defghij', headers={
            'Content-Length': '7',
            'Content-Range': 'bytes 3-9/10',
        })
        result.status_code = 206
        self.session.get.return_value = result
        temp = self.client.download_file('https://example.com/audio', path)
        self.assertEqual(temp.read_bytes(), b'abcdefghij')
        self.assertEqual(self.session.get.call_args.kwargs['headers'], {
            'Range': 'bytes=3-', 'Accept-Encoding': 'identity',
        })

    def test_invalid_range_response_discards_partial_file(self):
        path = Path(self.temp.name) / 'track.m4a'
        partial = path.with_name(path.name + '.part')
        partial.write_bytes(b'abc')
        result = response(b'data', headers={'Content-Range': 'bytes 0-3/7'})
        result.status_code = 206
        self.session.get.return_value = result
        with self.assertRaises(app.DownloadError):
            self.client.download_file('https://example.com/audio', path)
        self.assertFalse(partial.exists())

    def test_resume_restarts_when_server_ignores_range(self):
        path = Path(self.temp.name) / 'track.m4a'
        path.with_name(path.name + '.part').write_bytes(b'old partial')
        result = response(b'complete', headers={'Content-Length': '8'})
        result.status_code = 200
        self.session.get.return_value = result
        temp = self.client.download_file('https://example.com/audio', path)
        self.assertEqual(temp.read_bytes(), b'complete')

    def test_optional_lyrics_failure_keeps_track(self):
        self.prepare_track()
        self.client.metadata.return_value['123']['has_lyrics'] = True
        self.client.api = Mock(side_effect=app.DownloadError('lyrics offline'))
        self.client.processTrack('token')
        self.assertEqual(self.client.downloaded, 1)
        self.assertIn('lyrics unavailable', self.output.getvalue())

    def test_optional_artwork_failure_keeps_track(self):
        self.prepare_track()
        self.client.cover = True
        self.client.metadata.return_value['123']['image'] = 'https://example.com/image-150x150.jpg'
        self.session.get.side_effect = [requests.ConnectionError('cover offline'), response(FIXTURE.read_bytes())]
        self.client.processTrack('token')
        self.assertEqual(self.client.downloaded, 1)
        self.assertIn('cover unavailable', self.output.getvalue())
        self.assertIn('500x500', self.session.get.call_args_list[0].args[0])

    def test_collection_continues_after_failure(self):
        self.client.metadata = Mock(return_value={
            'listname': 'My Playlist', 'list_count': 3,
            'songs': [{'perma_url': URL}, {}, {'perma_url': URL}],
        })
        self.client.processTrack = Mock()
        self.client.process_collection('playlist', 'token')
        self.assertEqual(self.client.processTrack.call_count, 2)
        self.assertEqual(self.client.failed, 1)
        positions = {call.args[2:4] for call in self.client.processTrack.call_args_list}
        self.assertEqual(positions, {(1, 3), (3, 3)})

    def test_collection_downloads_tracks_concurrently(self):
        self.client.metadata = Mock(return_value={
            'title': 'My Album',
            'songs': [{'perma_url': URL} for _ in range(4)],
        })
        barrier = threading.Barrier(4, timeout=2)
        worker_threads = set()
        worker_sessions = []

        def process_track(*args, **kwargs):
            worker_threads.add(threading.current_thread().ident)
            barrier.wait()
            self.client._increment_counter('downloaded')

        def create_worker_session():
            worker_session = Mock()
            worker_sessions.append(worker_session)
            return worker_session

        self.client._provided_session = False
        self.client._new_session = Mock(side_effect=create_worker_session)
        self.client.processTrack = Mock(side_effect=process_track)
        self.client.process_collection('album', 'token')
        self.assertEqual(len(worker_threads), 4)
        self.assertEqual(self.client.downloaded, 4)
        self.assertEqual(len(worker_sessions), 4)
        self.assertTrue(all(session.close.called for session in worker_sessions))

    def test_progress_bar_tracks_collection_completion(self):
        self.client.metadata = Mock(return_value={
            'title': 'My Album',
            'songs': [{'perma_url': URL} for _ in range(4)],
        })
        self.client.processTrack = Mock()
        self.client.progress_enabled = True
        self.client.process_collection('album', 'token')
        output = self.output.getvalue()
        self.assertIn('Downloading [', output)
        self.assertIn('4/4', output)

    def test_collection_fetches_all_playlist_pages(self):
        pages = {
            None: {
                'listname': 'Long Playlist',
                'list_count': 5,
                'songs': [{'id': f'song-{index}', 'perma_url': URL} for index in range(2)],
            },
            2: {'songs': [{'id': f'song-{index}', 'perma_url': URL} for index in range(2, 4)]},
            3: {'songs': [{'id': 'song-4', 'perma_url': URL}]},
        }
        self.client.metadata = Mock(side_effect=lambda kind, token, page=None: pages[page])
        self.client.processTrack = Mock()
        self.client.process_collection('playlist', 'playlist-token')
        self.assertEqual(self.client.processTrack.call_count, 5)
        self.assertEqual(
            {call.kwargs['song_data']['id'] for call in self.client.processTrack.call_args_list},
            {f'song-{index}' for index in range(5)})
        self.assertEqual(
            self.client.metadata.call_args_list,
            [call('playlist', 'playlist-token'),
             call('playlist', 'playlist-token', page=2),
             call('playlist', 'playlist-token', page=3)])

    def test_album_and_playlist_download_all_tracks(self):
        self.prepare_track()
        for kind in ('album', 'playlist'):
            with self.subTest(kind=kind):
                collection = {'title': f'Test {kind}', 'primary_artists': 'Artist',
                              'songs': [{'perma_url': URL}, {'perma_url': URL}]}
                self.client.metadata.side_effect = lambda item_kind, token: (
                    {'songs': [dict(SONG)]} if item_kind == 'song' else collection)
                self.client.process_collection(kind, 'collection-token')
        paths = list(Path(self.temp.name).rglob('*.m4a'))
        self.assertEqual(len(paths), 4)
        self.assertEqual(sorted(MP4(path)['trkn'][0] for path in paths),
                         [(1, 2), (1, 2), (2, 2), (2, 2)])

    def test_auth_url_and_quality(self):
        self.client.api = Mock(return_value={'auth_url': 'https://web.example/path/web-file?sig=web'})
        self.assertEqual(self.client.getCdnURL('enc'), 'https://aac.example/path/web-file?sig=web')
        self.assertEqual(self.client.api.call_args.kwargs['bitrate'], '320')


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(app, 'config_path', return_value=Path(folder.name) / 'config.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mixed_url_types(self):
        with patch.object(app, 'Jiosaavn') as cls, contextlib.redirect_stdout(io.StringIO()):
            client = cls.return_value
            client.downloaded = client.skipped = client.failed = 0
            self.assertEqual(app.main([URL, 'https://jiosaavn.com/album/name/album-token',
                                       'https://jiosaavn.com/featured/name/playlist-token']), 0)
            client.processTrack.assert_called_once_with('token')
            self.assertEqual(client.process_collection.call_args_list,
                             [unittest.mock.call('album', 'album-token'),
                              unittest.mock.call('playlist', 'playlist-token')])

    def test_url_list_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'urls.txt'
            path.write_text('\ufeff# My songs\n\n' + URL + '\nhttps://jiosaavn.com/album/name/album-token\n', encoding='utf-8')
            self.assertEqual(list(app.input_urls(['first'], [str(path)])),
                             ['first', URL, 'https://jiosaavn.com/album/name/album-token'])
            with patch.object(app, 'Jiosaavn') as cls, contextlib.redirect_stdout(io.StringIO()):
                client = cls.return_value
                client.downloaded = client.skipped = client.failed = 0
                self.assertEqual(app.main(['--input-file', str(path)]), 0)
                client.processTrack.assert_called_once_with('token')
                client.process_collection.assert_called_once_with('album', 'album-token')

    def test_url_list_stdin(self):
        with patch.object(app.sys, 'stdin', io.StringIO('# Songs\n' + URL + '\n')):
            self.assertEqual(list(app.input_urls([], ['-'])), [URL])

    def test_empty_url_list_exit_status(self):
        with patch.object(app.sys, 'stdin', io.StringIO('# Empty list\n')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(['-i', '-']), 1)

    def test_missing_url_list_exit_status(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(['-i', str(Path(folder) / 'missing.txt')]), 1)

    def test_reject_invalid_options(self):
        for args in ([URL, '--timeout', '0'], [URL, '--timeout', 'nan'],
                     [URL, '--quality', '999'], [URL, '--quiet', '--verbose'], []):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                app.main(args)
            self.assertEqual(exc.exception.code, 2)

    def test_dry_run_flag_configures_client(self):
        with patch.object(app, 'Jiosaavn') as cls, contextlib.redirect_stdout(io.StringIO()):
            client = cls.return_value
            client.downloaded = client.skipped = client.failed = client.planned = 0
            self.assertEqual(app.main([URL, '--dry-run', '--quiet']), 0)
            self.assertEqual(cls.call_args.kwargs['dry_run'], True)
            self.assertEqual(cls.call_args.kwargs['quiet'], True)
            self.assertEqual(cls.call_args.kwargs['verbose'], False)

    def test_multiple_urls_and_exit_status(self):
        with patch.object(app, 'Jiosaavn') as cls, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            client = cls.return_value
            client.downloaded = client.skipped = client.failed = 0
            result = app.main(['https://example.com/bad', URL, '--no-cover'])
            self.assertEqual(result, 1)
            client.processTrack.assert_called_once_with('token')
            client.session.close.assert_called_once()

    def test_interrupt_exit_status(self):
        with patch.object(app, 'Jiosaavn') as cls, contextlib.redirect_stderr(io.StringIO()):
            cls.return_value.processTrack.side_effect = KeyboardInterrupt
            self.assertEqual(app.main([URL]), 130)
            cls.return_value.session.close.assert_called_once()


class FolderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'settings/config.json'
        patcher = patch.object(app, 'config_path', return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_folder_without_download(self):
        library = Path(self.folder.name) / 'Music Library'
        with patch.object(app, 'Jiosaavn') as cls, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--set-output', str(library)]), 0)
            cls.assert_not_called()
        self.assertEqual(app.saved_output(), str(library.resolve()))
        self.assertFalse(library.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_relative_setting_is_saved_as_absolute(self):
        self.assertEqual(app.save_output('My Music'), str(Path('My Music').resolve()))
        self.assertTrue(Path(app.saved_output()).is_absolute())

    def test_saved_folder_and_one_time_override(self):
        library = app.save_output(str(Path(self.folder.name) / 'Music'))
        for arguments, expected in (([URL], library), ([URL, '-o', 'Other'], 'Other')):
            with patch.object(app, 'Jiosaavn') as cls, contextlib.redirect_stdout(io.StringIO()):
                cls.return_value.failed = 0
                self.assertEqual(app.main(arguments), 0)
                self.assertEqual(cls.call_args.args[0], expected)
        self.assertEqual(app.saved_output(), library)

    def test_show_effective_folder(self):
        library = app.save_output(str(Path(self.folder.name) / 'Music'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(app.main(['--show-output']), 0)
        self.assertEqual(output.getvalue().strip(), library)

    def test_default_without_settings(self):
        self.assertEqual(app.saved_output(), 'Downloads')

    def test_invalid_settings_are_actionable_and_can_be_repaired(self):
        self.path.parent.mkdir()
        for data in ('{broken', '[]', '{"output": null}', '{"output": ""}'):
            self.path.write_text(data)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(['--show-output']), 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(['--show-output', '-o', self.folder.name]), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--set-output', self.folder.name]), 0)
        self.assertEqual(app.saved_output(), str(Path(self.folder.name).resolve()))

    def test_file_cannot_be_default_folder(self):
        file = Path(self.folder.name) / 'song.m4a'
        file.write_bytes(b'file')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(['--set-output', str(file)]), 1)
        self.assertFalse(self.path.exists())

    def test_empty_folder_is_rejected(self):
        for arguments in (['--set-output', ''], ['--show-output', '-o', '']):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(arguments), 1)


if __name__ == '__main__':
    unittest.main()
