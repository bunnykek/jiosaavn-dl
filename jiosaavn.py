#jiosaavn-dl
#made by bunny

import requests
import re
import os
import sys
import subprocess
from mutagen.mp4 import MP4, MP4Cover


#Jiosaavn API endpoints, credits goes to cyberboysumanjay
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
|  $$$$$$/                                                                                       --by bunny
 \______/                                                                                        
"""

def clear() -> None:
    """ Clears The Console Of All Text """
    os.system('clear' if os.name == 'posix' else 'cls')


#returns album/song/playlist id
def get_id(response,identity):
    return(re.search(f'\"{identity}\":\".*?\"',response.text).group().replace(f'\"{identity}\":\"',"").replace("\"",""))

#album json handler
def album_json_handler(json, album_path):
    for song in json["songs"]:
        download_song(json["songs"].index(song)+1,song,album_path,json["primary_artists"],len(json['songs']))

#playlist json handler :/
def playlist_json_handler(json,playlist_path):
    
    for song in json['songs']:
        
        #download the artwork
        subprocess.Popen(["ffmpeg", "-y", "-i", song["image"].replace("150","500"), 
                           "-c", "copy", os.path.join(playlist_path,"cover.jpg")], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT).wait()

        #download the track
        download_song(json['songs'].index(song)+1,song,playlist_path,song['primary_artists'],len(json['songs']))
        
        #deleting the artwork
        os.remove(os.path.join(playlist_path,"cover.jpg"))


#song downloader
def download_song(pos,json,path,album_artists,total=1):

    #sanitize
    json['song'] = sanitize(json['song'])
    json["album"] = sanitize(json["album"])
    json["primary_artists"] = sanitize(json["primary_artists"])
    json["music"] = sanitize(json["music"])

    #setting the song download path
    song_path = os.path.join(path,f"{pos}. {json['song']}.m4a")

    #checking if the song already exists in the directory
    if(os.path.exists(song_path)):
        print(f"{json['song']} already downloaded.")
    else:
        print(f"\nDownloading : {pos}. {json['song']}...")
        
        #checking if the song is available in the region, if yes then proceed to download else prompt the unavailability
        if 'media_preview_url' in json:
            link = "https://sdlhivkcdnems04.cdnsrv.jio.com/jiosaavn.cdn.jio.com"+(re.search(r"/\d{3}/\w+_96", json['media_preview_url'].replace("\\",""))).group().replace("_96","_320.mp4")
            subprocess.Popen(["ffmpeg", "-y", "-i", link,"-fflags", "+bitexact",
             "-flags:a", "+bitexact", "-c", "copy", song_path], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT).wait()
            print("Tagging metadata...")
            tagger(json, path,f"{pos}. {json['song']}.m4a",album_artists,pos,total)
            print("Done.")
        else:
            print("\nTrack unavailable in your region!")


#sanitizer
def sanitize(string):
    return (string.replace('&amp;','&').replace('&#039;',"'").replace('"',"'").replace('&quot;',"'"))



#Tags metadata to a track
def tagger(json,path,name,album_artists,pos=1,total=1):
    audio = MP4(os.path.join(path,name))
    audio["\xa9nam"] = json["song"]
    audio["\xa9alb"] = json["album"]
    audio["\xa9ART"] = json["primary_artists"]
    audio["\xa9wrt"] = json["music"]
    audio["aART"] = album_artists
    audio["\xa9day"] = json["year"]
    audio["----:TXXX:Record label"] = bytes(json["label"],'UTF-8')
    audio["cprt"] = json["copyright_text"]
    audio["----:TXXX:Release date"] = bytes(json["release_date"],'UTF-8')
    audio["----:TXXX:Language"] = bytes(json["language"].title(),'UTF-8') 
    audio["----:TXXX:Content advisory"] = bytes("Clean" if json["explicit_content"] == 0 else "Explicit",'UTF-8')
    audio["----:TXXX:URL"] = bytes(json["album_url"],'UTF-8')
    audio["trkn"] = [(pos,total)]
    
    #if the song has lyrics then tag else skip
    if(json["has_lyrics"]=="true"):
        lyric_json = requests.get(lyrics_api + json["id"]).json()
        audio["\xa9lyr"] = lyric_json["lyrics"].replace("<br>","\n")
    
    #cover artwork tag
    with open(os.path.join(path,"cover.jpg"), "rb") as f:
        audio["covr"] = [MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save() #tagging done

clear()

print(logo)

while(1):
    
    #takes url
    url = input("\nEnter the Album/Song/Playlist URL : ")
    

    #handles album URL
    if("/album/" in url):

        response = requests.get(url)
        album_id = get_id(response,"album_id")
        
        #getting json
        album_json = requests.get(album_api+album_id).json()

        #sanitization
        album_json["primary_artists"] = sanitize(album_json["primary_artists"])
        album_json['title'] = sanitize(album_json['title'])



        #setting the album path
        album_path = os.path.join(sys.path[0],"Downloads",(album_json["primary_artists"] if album_json["primary_artists"].count(",")<2 else "Various Artists") + f" - {album_json['title']} [{album_json['year']}]")
        try:
            os.makedirs(album_path)
        except:
            pass
        
        album_info = f"""
                            Album name       : {album_json['title']}
                            Album artists    : {album_json['primary_artists']}
                            Release date     : {album_json['release_date']}
                            Album ID         : {album_json['albumid']}
                            Number of tracks : {len(album_json['songs'])}
        """
        print(album_info)
        
        #checking if the cover already exists inside the album folder
        if not os.path.exists(os.path.join(album_path,"cover.jpg")):
            print("\nDownloading the cover...")
            subprocess.Popen(["ffmpeg", "-y", "-i", album_json["image"].replace("150","500"), 
                           "-c", "copy", os.path.join(album_path,"cover.jpg")], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT).wait()
        
        album_json_handler(album_json, album_path)
        

    #handles song URL
    elif("/song/" in url):
        song_response = requests.get(url)
        song_id = get_id(song_response,"song_id")
        song_json = requests.get(song_api+song_id).json()
        
        #getting json 
        song_json = song_json[f'{song_id}']

        #sanitize
        song_json["primary_artists"] = sanitize(song_json["primary_artists"])
        song_json['song'] = sanitize(song_json['song'])
        song_json['music'] = sanitize(song_json['music'])
        song_json['album'] = sanitize(song_json['album'])
        song_json['has_lyrics'] = sanitize(song_json['has_lyrics'])

        #setting up the song directory
        song_path = os.path.join(sys.path[0],"Downloads",(song_json["primary_artists"] if song_json["primary_artists"].count(",")<2 else "Various Artists") + f" - {song_json['song']} [{song_json['year']}]")
        try:
            os.makedirs(song_path)
        except:
            pass

        song_info = f"""
                            Song name      : {song_json['song']}
                            Artist(s) name : {song_json['primary_artists']}
                            Composer       : {song_json['music']}
                            Album name     : {song_json['album']}
                            Year           : {song_json['year']}
                            Has lyrics?    : {song_json['has_lyrics']}
        """

        print(song_info)
        
        #checking if the cover already exists
        if not os.path.exists(os.path.join(song_path,"cover.jpg")):
            print("\nDownloading the cover...")
            subprocess.Popen(["ffmpeg", "-y", "-i", song_json["image"].replace("150","500"), 
                           "-c", "copy", os.path.join(song_path,"cover.jpg")], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT).wait()
        
        download_song(1, song_json, song_path,song_json['primary_artists'])

    
    #handles playlist URL
    elif("/playlist/" in url):
        playlist_response = requests.get(url)
        playlist_id = get_id(playlist_response, "listid")
        
        #json
        playlist_json = requests.get(playlist_api+playlist_id).json()
        
        playlist_info = f"""
                            Playlist name    : {playlist_json['listname']}
                            Number of tracks : {playlist_json['list_count']}
        """
        print(playlist_info)
        #creating playlist directory
        try:
            os.makedirs(playlist_path)
        except:
            pass
        
        playlist_json_handler(playlist_json, playlist_path)
    
    else:
        print("Please enter a valid link!")
