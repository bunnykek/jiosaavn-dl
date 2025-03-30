# jiosaavn-dl
# made by bunny

import json
import requests
from html import unescape
from sanitize_filename import sanitize
import re
import os
import argparse
from mutagen.mp4 import MP4, MP4Cover


song_api = "https://www.jiosaavn.com/api.php?__call=webapi.get&token={}&type=song"
album_api = "https://www.jiosaavn.com/api.php?__call=webapi.get&token={}&type=album"
playlist_api = "https://www.jiosaavn.com/api.php?__call=webapi.get&token={}&type=playlist&_format=json&n=1000"
lyrics_api = "https://www.jiosaavn.com/api.php?__call=lyrics.getLyrics&ctx=web6dot0&api_version=4&_format=json&_marker=0%3F_marker%3D0&lyrics_id="
album_song_rx = re.compile("https://www\.jiosaavn\.com/(album|song)/.+?/(.+)")
playlist_rx = re.compile("https://www\.jiosaavn\.com/s/playlist/.+/(.+)")
json_rx = re.compile("({.+})")

logo = """
           /$$            /$$$$$$                                                         /$$ /$$
          |__/           /$$__  $$                                                       | $$| $$
       /$$ /$$  /$$$$$$ | $$  \__/  /$$$$$$   /$$$$$$  /$$    /$$ /$$$$$$$           /$$$$$$$| $$
      |__/| $$ /$$__  $$|  $$$$$$  |____  $$ |____  $$|  $$  /$$/| $$__  $$ /$$$$$$ /$$__  $$| $$
       /$$| $$| $$  \ $$ \____  $$  /$$$$$$$  /$$$$$$$ \  $$/$$/ | $$  \ $$|______/| $$  | $$| $$
      | $$| $$| $$  | $$ /$$  \ $$ /$$__  $$ /$$__  $$  \  $$$/  | $$  | $$        | $$  | $$| $$
      | $$| $$|  $$$$$$/|  $$$$$$/|  $$$$$$$|  $$$$$$$   \  $/   | $$  | $$        |  $$$$$$$| $$
      | $$|__/ \______/  \______/  \_______/ \_______/    \_/    |__/  |__/         \_______/|__/
 /$$  | $$                                                                                       
|  $$$$$$/                                                                             --by bunny
 \______/                                                                                        
"""


def clear() -> None:
    """ Clears The Console Of All Text """
    os.system('clear' if os.name == 'posix' else 'cls')


class Jiosaavn:
    def __init__(self) -> None:
        self.session = requests.Session()

    # Tags metadata to a track
    def tagger(self, json, song_path, album_artist, album_path, pos=1, total=1):
        audio = MP4(song_path)
        audio["\xa9nam"] = sanitize(unescape(json["song"]))
        audio["\xa9alb"] = sanitize(unescape(json["album"]))
        audio["\xa9ART"] = sanitize(unescape(json["primary_artists"]))
        audio["\xa9wrt"] = sanitize(unescape(json["music"]))
        audio["aART"] = album_artist if album_artist else sanitize(
            unescape(json["primary_artists"]))
        audio["\xa9day"] = json["release_date"]  # json["year"]
        audio["----:TXXX:Record label"] = bytes(json["label"], 'UTF-8')
        audio["cprt"] = json["copyright_text"]
        audio["----:TXXX:Language"] = bytes(json["language"].title(), 'UTF-8')
        audio["rtng"] = [2 if json["explicit_content"] == 0 else 4]
        # audio["----:TXXX:URL"] = bytes(json["album_url"], 'UTF-8')
        audio["trkn"] = [(pos, total)]

        # if the song has lyrics then tag else skip
        if(json["has_lyrics"] == "true"):
            lyric_json = self.session.get(lyrics_api + json["id"]).json()
            audio["\xa9lyr"] = lyric_json["lyrics"].replace("<br>", "\n")

        # cover artwork tag
        with open(os.path.join(album_path, "cover.jpg"), "rb") as f:
            audio["covr"] = [
                MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]

        # featured artists
        if len(json['featured_artists']) > 1:
            audio["----:TXXX:Featured artists"] = bytes(
                json["featured_artists"], 'UTF-8')

        # singers
        if len(json['singers']) > 1:
            audio["----:TXXX:Singers"] = bytes(json["singers"], 'UTF-8')

        # starring
        if len(json['starring']) > 1:
            audio["----:TXXX:Starring"] = bytes(json["starring"], 'UTF-8')

        audio.pop("©too")

        audio.save()  # tagging done

    def processAlbum(self, album_id):

        # sanitization
        album_json = self.session.get(album_api.format(album_id)).text
        album_json = json.loads(json_rx.search(album_json).group(1))

        album_name = sanitize(unescape(album_json['title']))
        album_artist = album_json['primary_artists']
        total_tracks = len(album_json['songs'])
        year = str(album_json['year'])

        album_info = f"\n\
                    Album info:\n\
                    Album name       : {album_name}\n\
                    Album artists    : {album_artist}\n\
                    Year             : {year}\n\
                    Number of tracks : {total_tracks}\n"
        print(album_info)

        song_pos = 1
        for song in album_json['songs']:
            song_id = album_song_rx.search(song['perma_url']).group(2)
            self.processTrack(song_id, album_artist, song_pos, total_tracks)
            song_pos += 1

    def processTrack(self, song_id, album_artist=None, song_pos=1, total_tracks=1, isPlaylist=False):

        metadata = self.session.get(song_api.format(song_id)).text
        metadata = json.loads(json_rx.search(metadata).group(1))
        # print(metadata.keys())
        song_json = metadata[f'{list(metadata.keys())[0]}']

        # sanitize
        primary_artists = album_artist if album_artist else sanitize(
            unescape(song_json["primary_artists"]))
        track_name = sanitize(unescape(song_json['song']))
        album = sanitize(unescape(song_json['album']))
        year = str(unescape(song_json['year']))

        # setting up the song directory
        if isPlaylist:
            folder_name = isPlaylist
        else:
            folder_name = f"{primary_artists if primary_artists.count(',') < 2 else 'Various Artists'} - {album} [{year}]"
        song_name = f"{str(song_pos).zfill(2)}. {track_name}.m4a"

        album_path = os.path.join("Downloads", folder_name)
        song_path = os.path.join("Downloads", folder_name, song_name)

        try:
            os.makedirs(album_path)
        except:
            pass

        song_info = f"\n\
                    Track info:\n\
                    Song name      : {song_json['song']}\n\
                    Artist(s) name : {song_json['primary_artists']}\n\
                    Album name     : {song_json['album']}\n\
                    Year           : {song_json['year']}\n"

        print(song_info)

        # checking if the cover already exists
        if not os.path.exists(os.path.join(album_path, "cover.jpg")) or isPlaylist:
            print("\nDownloading the cover...")
            with open(os.path.join(album_path, "cover.jpg"), "wb") as f:
                f.write(self.session.get(
                    song_json["image"].replace("150", "500")).content)

        # checking if the song already exists in the directory
        if(os.path.exists(song_path)):
            print(f"{song_name} already downloaded.")
        else:
            print(f"Downloading : {song_name}...")

            # checking if the song is available in the region, if yes then proceed to download else prompt the unavailability
            if 'media_preview_url' in song_json:
                cdnURL = self.getCdnURL(song_json["encrypted_media_url"])

                # download the song
                with open(song_path, "wb") as f:
                    f.write(self.session.get(cdnURL).content)

                print("Tagging metadata...")

                self.tagger(song_json, song_path, album_artist,
                            album_path, song_pos, total_tracks)
                print("Done.")
            else:
                print("\nTrack unavailable in your region!")

    def getCdnURL(self, encurl: str):
        params = {
            '__call': 'song.generateAuthToken',
            'url': encurl,
            'bitrate': '320',
            'api_version': '4',
            '_format': 'json',
            'ctx': 'web6dot0',
            '_marker': '0',
        }
        response = self.session.get('https://www.jiosaavn.com/api.php', params=params)
        return response.json()["auth_url"]


    def processPlaylist(self, playlist_id):
        # json

        playlist_json = self.session.get(playlist_api.format(playlist_id)).text
        playlist_json = json.loads(json_rx.search(playlist_json).group(1))
        print(json.dumps(playlist_json, indent=4))
        playlist_name = playlist_json['listname']
        total_tracks = int(playlist_json['list_count'])
        playlist_path = f"Playlist - {playlist_name}"
        playlist_info = f"\n\
                            Playlist info:\n\
                            Playlist name    : {playlist_name}\n\
                            Number of tracks : {total_tracks}\n"
        print(playlist_info)

        song_pos = 1
        for song in playlist_json['songs']:
            song_id = album_song_rx.search(song['perma_url']).group(2)
            self.processTrack(song_id, None, song_pos,
                            total_tracks, playlist_path)
            song_pos += 1


if __name__ == "__main__":
    clear()
    print(logo)

    parser = argparse.ArgumentParser(
        description="Downloads songs/albums/playlist from JioSaavn")
    parser.add_argument("url", help="Song/album/playlist URL")
    args = parser.parse_args()

    url = args.url

    jiosaavn = Jiosaavn()

    # handles album URL
    if("/album/" in url or "/song/" in url):

        kind, id_ = album_song_rx.search(url).groups()

        if kind == 'song':
            jiosaavn.processTrack(id_, None, 1, 1)
        elif kind == 'album':
            jiosaavn.processAlbum(id_)
    elif '/playlist/' in url:
        playlist_id = playlist_rx.search(url).group(1)
        jiosaavn.processPlaylist(playlist_id)
    else:
        print("Please enter a valid link!")
