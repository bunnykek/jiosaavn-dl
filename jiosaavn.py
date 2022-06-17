# jiosaavn-dl
# made by bunny

import requests
from html import unescape
import re
import os
import sys
import argparse
from mutagen.mp4 import MP4, MP4Cover


# Jiosaavn API endpoints, credits goes to cyberboysumanjay
song_api = "https://www.jiosaavn.com/api.php?__call=song.getDetails&cc=in&_marker=0%3F_marker%3D0&_format=json&pids="
album_api = "https://www.jiosaavn.com/api.php?__call=content.getAlbumDetails&_format=json&cc=in&_marker=0%3F_marker%3D0&albumid="
playlist_api = "https://www.jiosaavn.com/api.php?__call=playlist.getDetails&_format=json&cc=in&_marker=0%3F_marker%3D0&listid="
lyrics_api = "https://www.jiosaavn.com/api.php?__call=lyrics.getLyrics&ctx=web6dot0&api_version=4&_format=json&_marker=0%3F_marker%3D0&lyrics_id="


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


# returns album/song/playlist id
def get_id(response, identity):
    return(re.search(f'\"{identity}\":\".*?\"', response.text).group().replace(f'\"{identity}\":\"', "").replace("\"", ""))

# album json handler
def album_json_handler(json, album_path):
    for song in json["songs"]:
        download_song(json["songs"].index(song)+1, song, album_path,
                      json["primary_artists"], len(json['songs']))

# playlist json handler :/
def playlist_json_handler(json, playlist_path):

    for song in json['songs']:

        # download the artwork
        with open(os.path.join(playlist_path, "cover.jpg"), "wb") as f:
            f.write(requests.get(song["image"].replace("150", "500")).content)

        # download the track
        download_song(json['songs'].index(
            song)+1, song, playlist_path, song['primary_artists'], len(json['songs']))

        # deleting the artwork
        os.remove(os.path.join(playlist_path, "cover.jpg"))


# song downloader
def download_song(pos, json, path, album_artists, total=1):

    # sanitize
    json['song'] = unescape(json['song'])
    json["album"] = unescape(json["album"])
    json["primary_artists"] = unescape(json["primary_artists"])
    json["music"] = unescape(json["music"])

    # setting the song download path
    song_path = os.path.join(path, f"{str(pos).zfill(2)}. {json['song']}.m4a")

    # checking if the song already exists in the directory
    if(os.path.exists(song_path)):
        print(f"{json['song']} already downloaded.")
    else:
        print(f"\nDownloading : {str(pos).zfill(2)}. {json['song']}...")

        # checking if the song is available in the region, if yes then proceed to download else prompt the unavailability
        if 'media_preview_url' in json:
            link = "https://sdlhivkcdnems04.cdnsrv.jio.com/jiosaavn.cdn.jio.com" + \
                (re.search(r"/\d{3}/\w+_96", json['media_preview_url'].replace(
                    "\\", ""))).group().replace("_96", "_320.mp4")

            # download the song
            with open(song_path, "wb") as f:
                f.write(requests.get(link).content)

            print("Tagging metadata...")
            tagger(
                json, path, f"{str(pos).zfill(2)}. {json['song']}.m4a", album_artists, pos, total)
            print("Done.")
        else:
            print("\nTrack unavailable in your region!")


# Tags metadata to a track
def tagger(json, path, name, album_artists, pos=1, total=1):
    audio = MP4(os.path.join(path, name))
    audio["\xa9nam"] = json["song"]
    audio["\xa9alb"] = json["album"]
    audio["\xa9ART"] = json["primary_artists"]
    audio["\xa9wrt"] = json["music"]
    audio["aART"] = album_artists
    audio["\xa9day"] = json["release_date"]  # json["year"]
    audio["----:TXXX:Record label"] = bytes(json["label"], 'UTF-8')
    audio["cprt"] = json["copyright_text"]
    #audio["----:TXXX:Release date"] = bytes(json["release_date"], 'UTF-8')
    audio["----:TXXX:Language"] = bytes(json["language"].title(), 'UTF-8')
    audio["rtng"] = [2 if json["explicit_content"] == 0 else 4]
    audio["----:TXXX:URL"] = bytes(json["album_url"], 'UTF-8')
    audio["trkn"] = [(pos, total)]

    # if the song has lyrics then tag else skip
    if(json["has_lyrics"] == "true"):
        lyric_json = requests.get(lyrics_api + json["id"]).json()
        audio["\xa9lyr"] = lyric_json["lyrics"].replace("<br>", "\n")

    # cover artwork tag
    with open(os.path.join(path, "cover.jpg"), "rb") as f:
        audio["covr"] = [MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]
    audio.pop("©too")
    audio.save()  # tagging done


if __name__ == "__main__":
    clear()
    print(logo)

    parser = argparse.ArgumentParser(
        description="Downloads songs/albums/playlist from JioSaavn")
    parser.add_argument("url", help="Song/album/playlist URL")
    args = parser.parse_args()

    url = args.url

    # handles album URL
    if("/album/" in url):

        response = requests.get(url)
        album_id = get_id(response, "album_id")

        # getting json
        album_json = requests.get(album_api+album_id).json()

        # sanitization
        album_json["primary_artists"] = unescape(album_json["primary_artists"])
        album_json['title'] = unescape(album_json['title'])

        # setting the album path
        album_path = os.path.join(sys.path[0], "Downloads", (album_json["primary_artists"] if album_json["primary_artists"].count(
            ",") < 2 else "Various Artists") + f" - {album_json['title']} [{album_json['year']}]")
        try:
            os.makedirs(album_path)
        except:
            pass

        album_info = f"\n\
                    Album info:\n\
                    Album name       : {album_json['title']}\n\
                    Album artists    : {album_json['primary_artists']}\n\
                    Release date     : {album_json['release_date']}\n\
                    Album ID         : {album_json['albumid']}\n\
                    Number of tracks : {len(album_json['songs'])}\n"
        print(album_info)

        # checking if the cover already exists inside the album folder
        if not os.path.exists(os.path.join(album_path, "cover.jpg")):
            # download the cover
            print("\nDownloading the cover...")
            with open(os.path.join(album_path, "cover.jpg"), "wb") as f:
                f.write(requests.get(
                    album_json["image"].replace("150", "500")).content)

        album_json_handler(album_json, album_path)

    # handles song URL
    elif("/song/" in url):
        song_response = requests.get(url)
        song_id = get_id(song_response, "song_id")
        song_json = requests.get(song_api+song_id).json()

        # getting json
        song_json = song_json[f'{song_id}']

        # sanitize
        song_json["primary_artists"] = unescape(song_json["primary_artists"])
        song_json['song'] = unescape(song_json['song'])
        song_json['music'] = unescape(song_json['music'])
        song_json['album'] = unescape(song_json['album'])
        song_json['has_lyrics'] = unescape(song_json['has_lyrics'])

        # setting up the song directory
        song_path = os.path.join(sys.path[0], "Downloads", (song_json["primary_artists"] if song_json["primary_artists"].count(
            ",") < 2 else "Various Artists") + f" - {song_json['song']} [{song_json['year']}]")
        try:
            os.makedirs(song_path)
        except:
            pass

        song_info = f"\n\
                    Track info:\n\
                    Song name      : {song_json['song']}\n\
                    Artist(s) name : {song_json['primary_artists']}\n\
                    Composer       : {song_json['music']}\n\
                    Album name     : {song_json['album']}\n\
                    Year           : {song_json['year']}\n\
                    Has lyrics?    : {song_json['has_lyrics']}\n"

        print(song_info)

        # checking if the cover already exists
        if not os.path.exists(os.path.join(song_path, "cover.jpg")):
            print("\nDownloading the cover...")
            with open(os.path.join(song_path, "cover.jpg"), "wb") as f:
                f.write(requests.get(
                    song_json["image"].replace("150", "500")).content)

        download_song(1, song_json, song_path, song_json['primary_artists'])

    # handles playlist URL
    elif("/playlist/" in url):
        playlist_response = requests.get(url)
        playlist_id = get_id(playlist_response, "listid")

        # json
        playlist_json = requests.get(playlist_api+playlist_id).json()
        playlist_path = os.path.join(
            sys.path[0], "Downloads", f"Playlist - {playlist_json['listname']}")
        playlist_info = f"\n\
                        Playlist info:\n\
                        Playlist name    : {playlist_json['listname']}\n\
                        Number of tracks : {playlist_json['list_count']}\n"
        print(playlist_info)
        # creating playlist directory
        try:
            os.makedirs(playlist_path)
        except:
            pass

        playlist_json_handler(playlist_json, playlist_path)

    else:
        print("Please enter a valid link!")
