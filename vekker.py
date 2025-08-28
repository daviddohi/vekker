import wx
import wx.lib.newevent
import pygame
import datetime
import threading
import time
import json
import os
import sys
import logging
import copy
import wx.adv
import wx.lib.stattext # Statikus szöveg

# Google Drive API importok, ha szükségesek (csak akkor, ha a felhasználó telepítette őket)
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    from io import FileIO
    DRIVE_API_AVAILABLE = True
except ImportError:
    logging.warning("Google Drive API modulok nem találhatók. A Google Drive funkciók nem lesznek elérhetők.")
    DRIVE_API_AVAILABLE = False


# --- Logger beállítása ---
log_file_path = 'vekker_log.txt'
# Gyökér logger beállítása
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler(log_file_path, encoding='utf-8'),
                        logging.StreamHandler(sys.stdout)
                    ])

# --- Google Drive API konfiguráció ---
SCOPES = ['https://www.googleapis.com/auth/drive.file']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
DRIVE_FOLDER_NAME = 'Vekker_Backups'
SCHEDULE_FILE = 'csengetesi_rend.json'
SETTINGS_FILE = 'vekker_settings.json'



# ==================== AdaptiveVoiceDuckerVAD (WebRTC alapú) ====================
import pyaudio
import numpy as np
import webrtcvad
import logging
import threading
import time
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

class AdaptiveVoiceDuckerVAD:
    def __init__(self,
                 min_volume=0.15,
                 max_volume=1.0,
                 attack=0.35,
                 release=0.96,
                 check_interval=0.04,
                 vad_level=2):

        self.min_volume = min_volume
        self.max_volume = max_volume
        self.attack = attack
        self.release = release
        self.check_interval = check_interval
        self.running = False

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self.volume = cast(interface, POINTER(IAudioEndpointVolume))

        try:
            self.original_volume = float(self.volume.GetMasterVolumeLevelScalar())
        except Exception:
            logging.exception("Nem sikerült beolvasni az eredeti hangerőt, 1.0-t használunk.")
            self.original_volume = 1.0
        self.current_volume = self.original_volume

        self.audio = pyaudio.PyAudio()
        self.stream = self.audio.open(format=pyaudio.paInt16,
                                      channels=1,
                                      rate=16000,
                                      input=True,
                                      frames_per_buffer=320)  # 20ms @16kHz

        self.vad = webrtcvad.Vad(vad_level)

        logging.info(f"AdaptiveVoiceDuckerVAD inicializálva. original_volume={self.original_volume:.2f}")

    def _is_speech(self, frame: bytes) -> bool:
        try:
            return self.vad.is_speech(frame, 16000)
        except Exception:
            return False

    def _compute_target_volume(self, speech_detected: bool) -> float:
        if not speech_detected:
            return self.original_volume
        return self.min_volume

    def _smooth(self, current: float, target: float) -> float:
        alpha = self.attack if target < current else self.release
        return alpha * current + (1.0 - alpha) * target

    def _set_volume_safe(self, level: float):
        level = max(0.0, min(1.0, float(level)))
        try:
            self.volume.SetMasterVolumeLevelScalar(level, None)
        except Exception:
            logging.exception("Hangerő állítás hiba")

    def start(self):
        logging.info("AdaptiveVoiceDuckerVAD indul...")
        self.running = True
        threading.Thread(target=self._monitor, daemon=True).start()

    def stop(self):
        logging.info("AdaptiveVoiceDuckerVAD leáll, hangerő visszaállítva.")
        self._set_volume_safe(self.original_volume)
        self.running = False
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.audio.terminate()

    def _monitor(self):
        logging.info("AdaptiveVoiceDuckerVAD szál elindult.")
        while self.running:
            try:
                frame = self.stream.read(320, exception_on_overflow=False)
            except Exception:
                logging.exception("Mikrofon olvasási hiba")
                time.sleep(0.1)
                continue

            speech = self._is_speech(frame)
            target_volume = self._compute_target_volume(speech)
            prev = self.current_volume
            self.current_volume = self._smooth(self.current_volume, target_volume)
            self._set_volume_safe(self.current_volume)
            logging.debug(f"[DuckerVAD] speech={speech} target={target_volume:.2f} vol={self.current_volume:.2f} prev={prev:.2f}")
            time.sleep(self.check_interval)
        logging.info("AdaptiveVoiceDuckerVAD szál leállt.")
# =================================================================

# --- Segéd változók ---
WEEKDAYS_HUNGARIAN = ["Hétfő", "Kedd", "Szerda", "Csütörtök", "Péntek", "Szombat", "Vasárnap"]

# Egyedi esemény a hang lejátszás befejezéséhez
BellFinishedPlayingEvent, EVT_BELL_FINISHED_PLAYING = wx.lib.newevent.NewEvent()
# Egyedi esemény a Google Drive állapot frissítéséhez
DriveStatusEvent, EVT_DRIVE_STATUS = wx.lib.newevent.NewEvent()
# Egyedi esemény a csengetési lista frissítéséhez (pl. másolás után)
ScheduleUpdatedEvent, EVT_SCHEDULE_UPDATED = wx.lib.newevent.NewEvent()


class GoogleDriveManager:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.creds = None
        self.service = None
        self.lock = threading.Lock() # Zár a többszálas hozzáféréshez
        self.last_backup_time = None
        self.authenticated = False

    def _update_status(self, message, authenticated=None, last_backup=None):
        if authenticated is not None:
            self.authenticated = authenticated
        if last_backup is not None:
            self.last_backup_time = last_backup

        event = DriveStatusEvent(message=message, authenticated=self.authenticated, last_backup_time=self.last_backup_time)
        wx.PostEvent(self.main_frame, event)
        logging.info(f"Google Drive állapot frissítve: {message}")

    def _get_drive_service(self):
        if not DRIVE_API_AVAILABLE:
            self._update_status("Google Drive API modulok hiányoznak.", authenticated=False)
            return None

        with self.lock: # Zár biztosítja a szálbiztos hozzáférést a creds-hez és service-hez
            if self.creds and self.creds.valid and self.service:
                return self.service

            if os.path.exists(TOKEN_FILE):
                try:
                    self.creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
                except Exception as e:
                    logging.error(f"Hiba a tokenfájl betöltésekor: {e}")
                    self.creds = None

            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    try:
                        self.creds.refresh(Request())
                    except Exception as e:
                        logging.error(f"Hiba a token frissítésekor: {e}")
                        self.creds = None
                else:
                    try:
                        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                        self.creds = flow.run_local_server(port=0)
                    except Exception as e:
                        logging.error(f"Hiba a hitelesítés során: {e}")
                        self.creds = None

                if self.creds:
                    try:
                        with open(TOKEN_FILE, 'w') as token:
                            token.write(self.creds.to_json())
                    except Exception as e:
                        logging.error(f"Hiba a token mentésekor: {e}")

            if self.creds:
                self.service = build('drive', 'v3', credentials=self.creds)
                self.authenticated = True
                self._update_status("Sikeresen bejelentkezve.", authenticated=True)
                return self.service

            self.authenticated = False
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return None

    def authenticate_google_drive(self):
        logging.info("Google Drive hitelesítés indítása...")
        self._update_status("Hitelesítés folyamatban...", authenticated=False)
        threading.Thread(target=self._authenticate_thread, daemon=True).start()

    def _authenticate_thread(self):
        service = self._get_drive_service()
        if service:
            self._update_status("Sikeresen bejelentkezve.", authenticated=True)
            logging.info("Google Drive hitelesítés sikeres.")
        else:
            self._update_status("Hitelesítés sikertelen.", authenticated=False)
            logging.error("Google Drive hitelesítés sikertelen.")

    def sign_out_google_drive(self):
        if not DRIVE_API_AVAILABLE:
            self._update_status("Google Drive API modulok hiányoznak.", authenticated=False)
            return

        with self.lock:
            self.creds = None
            self.service = None
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            self.authenticated = False
            self.last_backup_time = None
            self._update_status("Kijelentkezve.", authenticated=False, last_backup=None)
            logging.info("Google Drive kijelentkezés sikeres.")

    def _find_or_create_folder(self, service):
        # Keresés meglévő mappa után
        query = f"name='{DRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = response.get('files', [])
        if files:
            logging.info(f"Meglévő Google Drive mappa található: {files[0]['name']} (ID: {files[0]['id']})")
            return files[0]['id']
        else:
            # Ha nem található, létrehozás
            file_metadata = {
                'name': DRIVE_FOLDER_NAME,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = service.files().create(body=file_metadata, fields='id').execute()
            logging.info(f"Google Drive mappa létrehozva: {DRIVE_FOLDER_NAME} (ID: {folder.get('id')})")
            return folder.get('id')

    def list_drive_files(self):
        if not self.authenticated:
            self.authenticate_google_drive() # Megpróbáljuk hitelesíteni, ha nincs bejelentkezve
            return # A listázás a hitelesítés után fog lefutni

        logging.info("Google Drive fájlok listázása indult...")
        self._update_status("Fájlok lekérése...", authenticated=True)
        threading.Thread(target=self._list_drive_files_thread, daemon=True).start()

    def _list_drive_files_thread(self):
        service = self._get_drive_service()
        if not service:
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        try:
            folder_id = self._find_or_create_folder(service)
            if not folder_id:
                self._update_status("Mappa létrehozása/keresése sikertelen.", authenticated=True)
                return

            query = f"'{folder_id}' in parents and trashed=false"
            response = service.files().list(q=query, spaces='drive', fields='files(id, name, modifiedTime, size)').execute()
            files = response.get('files', [])
            logging.info(f"Google Drive fájlok lekérve. Találatok: {len(files)}")
            self._update_status(f"Fájlok lekérve. ({len(files)} találat)", authenticated=True)
            wx.CallAfter(self.main_frame.settings_panel.update_drive_file_list, files)

        except Exception as e:
            logging.error(f"Hiba a Google Drive fájlok listázásakor: {e}")
            self._update_status(f"Hiba a fájlok listázásakor: {e}", authenticated=True)

    def upload_file_to_drive(self, local_file_path):
        if not self.authenticated:
            logging.warning("Nincs bejelentkezve a Google Drive-ba. Fájl feltöltés sikertelen.")
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        logging.info(f"Fájl feltöltése Google Drive-ra indult: {local_file_path}")
        self._update_status(f"Feltöltés folyamatban: {os.path.basename(local_file_path)}...", authenticated=True)
        threading.Thread(target=self._upload_file_to_drive_thread, args=(local_file_path,), daemon=True).start()

    def _upload_file_to_drive_thread(self, local_file_path):
        service = self._get_drive_service()
        if not service:
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        try:
            folder_id = self._find_or_create_folder(service)
            if not folder_id:
                self._update_status("Mappa létrehozása/keresése sikertelen.", authenticated=True)
                return

            file_name = os.path.basename(local_file_path)

            # Ellenőrizzük, hogy létezik-e már ilyen nevű fájl a mappában
            query = f"name='{file_name}' and '{folder_id}' in parents and trashed=false"
            response = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
            existing_files = response.get('files', [])

            file_metadata = {
                'name': file_name,
                'parents': [folder_id]
            }
            media = MediaFileUpload(local_file_path, resumable=True)

            if existing_files:
                # Frissítjük a meglévő fájlt
                file_id = existing_files[0]['id']
                service.files().update(fileId=file_id, media_body=media).execute()
                logging.info(f"Google Drive fájl frissítve: {file_name}")
                self._update_status(f"Fájl frissítve: {file_name}", authenticated=True, last_backup=datetime.datetime.now())
            else:
                # Létrehozzuk az új fájlt
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                logging.info(f"Google Drive fájl feltöltve: {file_name}")
                self._update_status(f"Fájl feltöltve: {file_name}", authenticated=True, last_backup=datetime.datetime.now())

        except Exception as e:
            logging.error(f"Hiba a Google Drive feltöltés során: {e}")
            self._update_status(f"Feltöltési hiba: {e}", authenticated=True)

    def download_file_from_drive(self, file_id, file_name, local_path):
        if not self.authenticated:
            logging.warning("Nincs bejelentkezve a Google Drive-ba. Fájl letöltés sikertelen.")
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        logging.info(f"Fájl letöltése Google Drive-ról indult: {file_name} (ID: {file_id})")
        self._update_status(f"Letöltés folyamatban: {file_name}...", authenticated=True)
        threading.Thread(target=self._download_file_from_drive_thread, args=(file_id, file_name, local_path), daemon=True).start()

    def _download_file_from_drive_thread(self, file_id, file_name, local_path):
        service = self._get_drive_service()
        if not service:
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        try:
            request = service.files().get_media(fileId=file_id)
            with open(local_path, 'wb') as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while done is False:
                    status, done = downloader.next_chunk()
            logging.info(f"Google Drive fájl letöltve: {local_path}")
            self._update_status(f"Fájl letöltve: {file_name}", authenticated=True)
            # Sikeres letöltés után töltse be a schedule-t vagy settings-t
            if file_name == SCHEDULE_FILE:
                wx.CallAfter(self.main_frame.load_bell_schedule)
            elif file_name == SETTINGS_FILE:
                wx.CallAfter(self.main_frame.load_settings)
        except Exception as e:
            logging.error(f"Hiba a Google Drive letöltés során: {e}")
            self._update_status(f"Letöltési hiba: {e}", authenticated=True)

class SettingsManager:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.settings_file = SETTINGS_FILE
        self.settings = self.load_settings()

    def load_settings(self):
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    logging.info("Beállítások betöltve.")
                    return settings
            except Exception as e:
                logging.error(f"Hiba a beállítások betöltésekor: {e}")
        # Alapértelmezett értékek
        return {
            'volume': 50,
            'check_interval': 5.0,
            'ducking_enabled': False # Új beállítás
        }

    def save_settings(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4)
            logging.info("Beállítások elmentve.")
            self.main_frame.show_status_message("Beállítások elmentve.")
            # Feltöltés Google Drive-ra is, ha be van jelentkezve
            if self.main_frame.drive_manager.authenticated:
                self.main_frame.drive_manager.upload_file_to_drive(self.settings_file)
        except Exception as e:
            logging.error(f"Hiba a beállítások mentésekor: {e}")
            self.main_frame.show_status_message(f"Hiba a beállítások mentésekor: {e}")

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value
        self.save_settings()

class BellScheduleManager:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.schedule_file = SCHEDULE_FILE
        self.bell_schedule = self.load_bell_schedule()

    def load_bell_schedule(self):
        if os.path.exists(self.schedule_file):
            try:
                with open(self.schedule_file, 'r', encoding='utf-8') as f:
                    schedule = json.load(f)
                    logging.info("Ütemezés betöltve: csengetesi_rend.json")
                    self.main_frame.show_status_message("Ütemezés betöltve: csengetesi_rend.json")
                    return schedule
            except Exception as e:
                logging.error(f"Hiba az csengetési rend betöltésekor: {e}")
        logging.info("Nincs meglévő csengetési rend fájl, üres lista indul.")
        return []

    def save_bell_schedule(self):
        try:
            # Rendezzük az csengetéseket idő szerint mentés előtt
            self.bell_schedule.sort(key=lambda x: datetime.datetime.strptime(x['time'], "%H:%M").time())
            with open(self.schedule_file, 'w', encoding='utf-8') as f:
                json.dump(self.bell_schedule, f, indent=4)
            logging.info("Csengetési rend elmentve.")
            self.main_frame.show_status_message("Csengetési rend elmentve.")
            # Feltöltés Google Drive-ra is, ha be van jelentkezve
            if self.main_frame.drive_manager.authenticated:
                self.main_frame.drive_manager.upload_file_to_drive(self.schedule_file)
        except Exception as e:
            logging.error(f"Hiba az csengetési rend mentésekor: {e}")
            self.main_frame.show_status_message(f"Hiba az csengetési rend mentésekor: {e}")

    def add_bell(self, bell_data):
        self.bell_schedule.append(bell_data)
        logging.info(f"Új csengetés hozzáadva: {bell_data['time']}")
        self.save_bell_schedule()

    def update_bell(self, index, new_bell_data):
        if 0 <= index < len(self.bell_schedule):
            self.bell_schedule[index] = new_bell_data
            logging.info(f"Csengetés frissítve (index: {index}): {new_bell_data['time']}")
            self.save_bell_schedule()
            return True
        return False

    def delete_bell(self, index):
        if 0 <= index < len(self.bell_schedule):
            deleted_bell = self.bell_schedule.pop(index)
            logging.info(f"Csengetés törölve (index: {index}): {deleted_bell['time']}")
            self.save_bell_schedule()
            return True
        return False

    def get_bell_by_index(self, index):
        if 0 <= index < len(self.bell_schedule):
            return copy.deepcopy(self.bell_schedule[index])
        return None

    def get_bells_for_day(self, day):
        # Ha "Összes nap" van kiválasztva, visszaadjuk az összes csengetést
        if day == "Összes nap":
            return self.bell_schedule
        return [bell for bell in self.bell_schedule if day in bell.get('weekdays', [])]

    def copy_bells_to_days(self, source_day, destination_days):
        if source_day not in WEEKDAYS_HUNGARIAN:
            logging.error(f"Érvénytelen forrás nap: {source_day}")
            return

        bells_to_copy = self.get_bells_for_day(source_day)
        added_count = 0
        skipped_count = 0

        for dest_day in destination_days:
            if dest_day not in WEEKDAYS_HUNGARIAN:
                logging.warning(f"Érvénytelen cél nap kihagyva: {dest_day}")
                continue
            if dest_day == source_day:
                logging.info(f"Forrás és cél nap megegyezik ({dest_day}), kihagyva a másolást ide.")
                continue

            for bell in bells_to_copy:
                # Létrehozunk egy mély másolatot az csengetésről
                new_bell = copy.deepcopy(bell)

                # Frissítjük a napokat az új napra
                new_bell['weekdays'] = [dest_day]

                # Ellenőrizzük, hogy létezik-e már pontosan ilyen csengetés a cél napon
                # (idő, hangfájl és név egyezik az adott napra)
                already_exists = False
                for existing_bell in self.get_bells_for_day(dest_day):
                    if (existing_bell['time'] == new_bell['time'] and
                        existing_bell.get('name') == new_bell.get('name') and
                        existing_bell.get('sound_file') == new_bell.get('sound_file')):
                        already_exists = True
                        break

                if not already_exists:
                    self.bell_schedule.append(new_bell)
                    added_count += 1
                    logging.info(f"Csengetés másolva: {new_bell['time']} - {new_bell.get('name')} ide: {dest_day}")
                else:
                    skipped_count += 1
                    logging.info(f"Duplikátum csengetés kihagyva: {new_bell['time']} - {new_bell.get('name')} ide: {dest_day}")

        if added_count > 0:
            self.save_bell_schedule()
            wx.PostEvent(self.main_frame, ScheduleUpdatedEvent()) # Frissítjük a UI-t
            self.main_frame.show_status_message(f"{added_count} csengetés másolva. {skipped_count} kihagyva.")
            logging.info(f"Sikeresen másolva {added_count} csengetés, {skipped_count} duplikátum kihagyva.")
        else:
            self.main_frame.show_status_message("Nincs új csengetés másolva.")
            logging.info("Nincs új csengetés másolva.")


class BellPlayer:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.is_playing = False
        self.stop_requested = False
        self.current_sound_file = None
        self.player_thread = None

        try:
            pygame.mixer.init()
            logging.info("Pygame mixer inicializálva.")
        except Exception as e:
            logging.error(f"Hiba a Pygame mixer inicializálásakor: {e}")

    def play_sound(self, sound_file, volume):
        if not pygame.mixer.get_init():
            logging.error("Pygame mixer nincs inicializálva. A hang lejátszása sikertelen.")
            self.main_frame.show_status_message("Hiba: Hang lejátszás nem lehetséges (mixer hiba).")
            return

        if self.is_playing:
            self.stop_sound()
            time.sleep(0.1) # Várjunk egy kicsit, hogy a stop végrehajtódjon

        self.stop_requested = False
        self.current_sound_file = sound_file

        self.player_thread = threading.Thread(target=self._play_sound_thread, args=(sound_file, volume), daemon=True)
        self.player_thread.start()

    def _play_sound_thread(self, sound_file, volume):
        if not os.path.exists(sound_file):
            logging.error(f"A hangfájl nem található: {sound_file}")
            wx.CallAfter(self.main_frame.show_status_message, f"Hiba: A hangfájl nem található: {os.path.basename(sound_file)}")
            self.is_playing = False
            return

        try:
            pygame.mixer.music.load(sound_file)
            pygame.mixer.music.set_volume(volume / 100.0)
            pygame.mixer.music.play()
            self.is_playing = True
            logging.info(f"Hang lejátszása indult: {sound_file}, hangerő: {volume}")
            wx.CallAfter(self.main_frame.show_status_message, f"Csengetés szól: {os.path.basename(sound_file)}")

            while pygame.mixer.music.get_busy() and not self.stop_requested:
                time.sleep(0.1)

            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
            self.is_playing = False
            self.current_sound_file = None
            logging.info("Hang lejátszás befejeződött.")
            wx.CallAfter(self.main_frame.show_status_message, "Csengetés befejeződött.")
            wx.PostEvent(self.main_frame, BellFinishedPlayingEvent())

        except pygame.error as e:
            logging.error(f"Hiba a Pygame hang lejátszásakor: {e}")
            wx.CallAfter(self.main_frame.show_status_message, f"Hiba a hang lejátszásakor: {e}")
            self.is_playing = False
        except Exception as e:
            logging.error(f"Ismeretlen hiba a hang lejátszásakor: {e}")
            wx.CallAfter(self.main_frame.show_status_message, f"Ismeretlen hiba a hang lejátszásakor: {e}")
            self.is_playing = False


    def stop_sound(self):
        if self.is_playing:
            self.stop_requested = True
            logging.info("Hang lejátszás leállítási kérelem elküldve.")
            # Nem hívjuk meg itt a stop() és unload() fv-t,
            # mert a lejátszó szál felelős ezek végrehajtásáért.
            # Ehelyett várunk, hogy a szál befejeződjön.
            if self.player_thread and self.player_thread.is_alive():
                self.player_thread.join(timeout=1) # Max 1 mp-et várunk
                if self.player_thread.is_alive():
                    logging.warning("Lejátszó szál nem állt le időben.")
            self.is_playing = False
            self.current_sound_file = None
            logging.info("Hang lejátszás leállítva.")
            wx.CallAfter(self.main_frame.show_status_message, "Csengetés leállítva.")

class BellChecker:
    def __init__(self, main_frame, bell_player, bell_schedule_manager, settings_manager):
        self.main_frame = main_frame
        self.bell_player = bell_player
        self.bell_schedule_manager = bell_schedule_manager
        self.settings_manager = settings_manager
        self.timer = wx.Timer(main_frame)
        self.check_interval = self.settings_manager.get_setting('check_interval', 5.0)
        self.is_running = False
        self.thread = None
        self.stop_event = threading.Event()

    def start_checking(self):
        if self.is_running:
            return

        self.stop_event.clear()
        self.thread = threading.Thread(target=self._check_bells_thread, daemon=True)
        self.thread.start()
        self.is_running = True
        logging.info(f"Időzítő elindítva, ellenőrzési intervallum: {self.check_interval} másodperc.")
        logging.info("Ébresztő ellenőrző szál elindítva.")

    def stop_checking(self):
        if not self.is_running:
            return

        self.stop_event.set()
        logging.info("Ébresztő ellenőrző szál leállítási kérelem elküldve.")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=self.check_interval + 1) # Várjuk meg a szál leállását
            if self.thread.is_alive():
                logging.warning("Ébresztő ellenőrző szál nem állt le időben.")
        self.is_running = False
        logging.info("Ébresztő ellenőrző szál leállítva.")


    def _check_bells_thread(self):
        while not self.stop_event.is_set():
            now = datetime.datetime.now()
            current_time_str = now.strftime("%H:%M")
            current_weekday_index = now.weekday() # Hétfő=0, Vasárnap=6

            # Frissítsük a bell_schedule-t minden ellenőrzés előtt,
            # ha változott a fájl vagy a memória
            bell_schedule_copy = copy.deepcopy(self.bell_schedule_manager.bell_schedule)

            for bell in bell_schedule_copy:
                bell_time = bell['time']
                bell_weekdays = bell.get('weekdays', [])
                bell_name = bell.get('name', 'Névtelen csengetés')
                bell_sound_file = bell.get('sound_file')
                bell_volume = bell.get('volume', 50)
                bell_enabled = bell.get('enabled', True) # Alapértelmezett, hogy engedélyezve van

                if not bell_enabled:
                    continue # Kihagyjuk a letiltott csengetéseket

                # Ellenőrizzük a napokat
                if bell_weekdays and WEEKDAYS_HUNGARIAN[current_weekday_index] not in bell_weekdays:
                    continue

                if current_time_str == bell_time:
                    logging.info(f"Ébresztő szól: {bell_name} - {bell_time}")
                    if bell_sound_file:
                        full_sound_path = os.path.join('hangok', bell_sound_file) # Teljes elérési út
                        self.bell_player.play_sound(full_sound_path, bell_volume)
                    else:
                        wx.CallAfter(self.main_frame.show_status_message, f"Ébresztő szól: {bell_name} - {bell_time} (Nincs hangfájl beállítva)")
                    # Hogy ne szólaljon meg újra azonnal:
                    time.sleep(61) # Vár egy percet, mielőtt újra ellenőriz

            self.stop_event.wait(self.check_interval) # Vár a beállított intervallumot, vagy amíg meg nem állítják

    def update_check_interval(self, new_interval):
        self.check_interval = new_interval
        if self.is_running:
            self.stop_checking()
            self.start_checking()
        logging.info(f"Időzítő ellenőrzési intervallum frissítve: {new_interval} másodperc.")


class BellScheduleDialog(wx.Dialog):
    def __init__(self, parent, bell_data=None, available_sounds=None):
        super(BellScheduleDialog, self).__init__(parent, title="Csengetés hozzáadása", size=(400, 450))

        self.panel = wx.Panel(self)
        self.bell_data = bell_data if bell_data else {}
        self.available_sounds = available_sounds if available_sounds else []

        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Idő
        time_sizer = wx.BoxSizer(wx.HORIZONTAL)
        time_sizer.Add(wx.StaticText(self.panel, label="Idő:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        # Óra és perc kiválasztása külön legördülőkből
        time_sizer = wx.BoxSizer(wx.HORIZONTAL)
        time_sizer.Add(wx.StaticText(self.panel, label="Óra:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.hour_choice = wx.Choice(self.panel, choices=[f"{h:02d}" for h in range(24)])
        time_sizer.Add(self.hour_choice, 0, wx.ALL, 5)
        time_sizer.Add(wx.StaticText(self.panel, label="Perc:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.minute_choice = wx.Choice(self.panel, choices=[f"{m:02d}" for m in range(60)])
        time_sizer.Add(self.minute_choice, 0, wx.ALL, 5)
        main_sizer.Add(time_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # Név

        # Hangerő
        volume_sizer = wx.BoxSizer(wx.HORIZONTAL)
        volume_sizer.Add(wx.StaticText(self.panel, label="Hangerő:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.volume_slider = wx.Slider(self.panel, value=self.bell_data.get('volume', 50), minValue=0, maxValue=100,
                                      style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        volume_sizer.Add(self.volume_slider, 1, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(volume_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # Hangfájl
        sound_sizer = wx.BoxSizer(wx.HORIZONTAL)
        sound_sizer.Add(wx.StaticText(self.panel, label="Hangfájl:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.sound_choice = wx.Choice(self.panel, choices=self.available_sounds)
        sound_sizer.Add(self.sound_choice, 1, wx.EXPAND | wx.ALL, 5)
        test_sound_btn = wx.Button(self.panel, label="Teszt")
        test_sound_btn.Bind(wx.EVT_BUTTON, self.on_test_sound)
        sound_sizer.Add(test_sound_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
        main_sizer.Add(sound_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # Napok kiválasztása
        days_label = wx.StaticText(self.panel, label="Napok:")
        main_sizer.Add(days_label, 0, wx.ALL, 5)

        grid_sizer = wx.GridSizer(4, 2, 5, 5) # 4 sor, 2 oszlop, 5px vert és horiz. távolság
        self.day_checkboxes = {}
        for day in WEEKDAYS_HUNGARIAN:
            checkbox = wx.CheckBox(self.panel, label=day)
            grid_sizer.Add(checkbox, 0, wx.EXPAND)
            self.day_checkboxes[day] = checkbox
        main_sizer.Add(grid_sizer, 1, wx.EXPAND | wx.ALL, 5)

        # Gombok
        button_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(self.panel, wx.ID_OK, label="Mentés") # Label hozzáadva
        cancel_btn = wx.Button(self.panel, wx.ID_CANCEL)
        button_sizer.AddButton(ok_btn)
        button_sizer.AddButton(cancel_btn)
        button_sizer.Realize()

        main_sizer.Add(button_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        self.panel.SetSizer(main_sizer)
        self.panel.Layout() # Elrendezés frissítése

        if bell_data:
            self._load_bell_data(bell_data)
        else:
            if available_sounds:
                self.sound_choice.SetSelection(0) # Válassza ki az első hangot alapértelmezettként


    def _load_bell_data(self, bell_data):
        time_obj = datetime.datetime.strptime(bell_data['time'], "%H:%M").time()
        self.time_picker.SetValue(wx.DateTime.FromTime(time_obj.hour, time_obj.minute, 0))

        self.name_text.SetValue(bell_data.get('name', ''))
        self.volume_slider.SetValue(bell_data.get('volume', 50))

        try:
            index = self.sound_choice.FindString(bell_data.get('sound_file', ''))
            if index != wx.NOT_FOUND:
                self.sound_choice.SetSelection(index)
            else:
                wx.MessageBox(f"A korábbi hangfájl ({bell_data['sound_file']}) nem található. Kérjük válasszon újat.", "Hiányzó hangfájl", wx.OK | wx.ICON_WARNING)
        except Exception as e:
            logging.error(f"Hiba a hang...
---
```python
import wx
import wx.lib.newevent
import pygame
import datetime
import threading
import time
import json
import os
import sys
import logging
import copy
import wx.adv
import wx.lib.stattext # Statikus szöveg

# Google Drive API importok, ha szükségesek (csak akkor, ha a felhasználó telepítette őket)
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    from io import FileIO
    DRIVE_API_AVAILABLE = True
except ImportError:
    logging.warning("Google Drive API modulok nem találhatók. A Google Drive funkciók nem lesznek elérhetők.")
    DRIVE_API_AVAILABLE = False


# --- Logger beállítása ---
log_file_path = 'vekker_log.txt'
# Gyökér logger beállítása
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler(log_file_path, encoding='utf-8'),
                        logging.StreamHandler(sys.stdout)
                    ])

# --- Google Drive API konfiguráció ---
SCOPES = ['[https://www.googleapis.com/auth/drive.file](https://www.googleapis.com/auth/drive.file)']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'
DRIVE_FOLDER_NAME = 'Vekker_Backups'
SCHEDULE_FILE = 'csengetesi_rend.json'
SETTINGS_FILE = 'vekker_settings.json'



# ==================== AdaptiveVoiceDuckerVAD (WebRTC alapú) ====================
import pyaudio
import numpy as np
import webrtcvad
import logging
import threading
import time
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

class AdaptiveVoiceDuckerVAD:
    def __init__(self,
                 min_volume=0.15,
                 max_volume=1.0,
                 attack=0.35,
                 release=0.96,
                 check_interval=0.04,
                 vad_level=2):

        self.min_volume = min_volume
        self.max_volume = max_volume
        self.attack = attack
        self.release = release
        self.check_interval = check_interval
        self.running = False

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self.volume = cast(interface, POINTER(IAudioEndpointVolume))

        try:
            self.original_volume = float(self.volume.GetMasterVolumeLevelScalar())
        except Exception:
            logging.exception("Nem sikerült beolvasni az eredeti hangerőt, 1.0-t használunk.")
            self.original_volume = 1.0
        self.current_volume = self.original_volume

        self.audio = pyaudio.PyAudio()
        self.stream = self.audio.open(format=pyaudio.paInt16,
                                      channels=1,
                                      rate=16000,
                                      input=True,
                                      frames_per_buffer=320)  # 20ms @16kHz

        self.vad = webrtcvad.Vad(vad_level)

        logging.info(f"AdaptiveVoiceDuckerVAD inicializálva. original_volume={self.original_volume:.2f}")

    def _is_speech(self, frame: bytes) -> bool:
        try:
            return self.vad.is_speech(frame, 16000)
        except Exception:
            return False

    def _compute_target_volume(self, speech_detected: bool) -> float:
        if not speech_detected:
            return self.original_volume
        return self.min_volume

    def _smooth(self, current: float, target: float) -> float:
        alpha = self.attack if target < current else self.release
        return alpha * current + (1.0 - alpha) * target

    def _set_volume_safe(self, level: float):
        level = max(0.0, min(1.0, float(level)))
        try:
            self.volume.SetMasterVolumeLevelScalar(level, None)
        except Exception:
            logging.exception("Hangerő állítás hiba")

    def start(self):
        logging.info("AdaptiveVoiceDuckerVAD indul...")
        self.running = True
        threading.Thread(target=self._monitor, daemon=True).start()

    def stop(self):
        logging.info("AdaptiveVoiceDuckerVAD leáll, hangerő visszaállítva.")
        self._set_volume_safe(self.original_volume)
        self.running = False
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.audio.terminate()

    def _monitor(self):
        logging.info("AdaptiveVoiceDuckerVAD szál elindult.")
        while self.running:
            try:
                frame = self.stream.read(320, exception_on_overflow=False)
            except Exception:
                logging.exception("Mikrofon olvasási hiba")
                time.sleep(0.1)
                continue

            speech = self._is_speech(frame)
            target_volume = self._compute_target_volume(speech)
            prev = self.current_volume
            self.current_volume = self._smooth(self.current_volume, target_volume)
            self._set_volume_safe(self.current_volume)
            logging.debug(f"[DuckerVAD] speech={speech} target={target_volume:.2f} vol={self.current_volume:.2f} prev={prev:.2f}")
            time.sleep(self.check_interval)
        logging.info("AdaptiveVoiceDuckerVAD szál leállt.")
# =================================================================

# --- Segéd változók ---
WEEKDAYS_HUNGARIAN = ["Hétfő", "Kedd", "Szerda", "Csütörtök", "Péntek", "Szombat", "Vasárnap"]

# Egyedi esemény a hang lejátszás befejezéséhez
BellFinishedPlayingEvent, EVT_BELL_FINISHED_PLAYING = wx.lib.newevent.NewEvent()
# Egyedi esemény a Google Drive állapot frissítéséhez
DriveStatusEvent, EVT_DRIVE_STATUS = wx.lib.newevent.NewEvent()
# Egyedi esemény a csengetési lista frissítéséhez (pl. másolás után)
ScheduleUpdatedEvent, EVT_SCHEDULE_UPDATED = wx.lib.newevent.NewEvent()


class GoogleDriveManager:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.creds = None
        self.service = None
        self.lock = threading.Lock() # Zár a többszálas hozzáféréshez
        self.last_backup_time = None
        self.authenticated = False

    def _update_status(self, message, authenticated=None, last_backup=None):
        if authenticated is not None:
            self.authenticated = authenticated
        if last_backup is not None:
            self.last_backup_time = last_backup

        event = DriveStatusEvent(message=message, authenticated=self.authenticated, last_backup_time=self.last_backup_time)
        wx.PostEvent(self.main_frame, event)
        logging.info(f"Google Drive állapot frissítve: {message}")

    def _get_drive_service(self):
        if not DRIVE_API_AVAILABLE:
            self._update_status("Google Drive API modulok hiányoznak.", authenticated=False)
            return None

        with self.lock: # Zár biztosítja a szálbiztos hozzáférést a creds-hez és service-hez
            if self.creds and self.creds.valid and self.service:
                return self.service

            if os.path.exists(TOKEN_FILE):
                try:
                    self.creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
                except Exception as e:
                    logging.error(f"Hiba a tokenfájl betöltésekor: {e}")
                    self.creds = None

            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    try:
                        self.creds.refresh(Request())
                    except Exception as e:
                        logging.error(f"Hiba a token frissítésekor: {e}")
                        self.creds = None
                else:
                    try:
                        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                        self.creds = flow.run_local_server(port=0)
                    except Exception as e:
                        logging.error(f"Hiba a hitelesítés során: {e}")
                        self.creds = None

                if self.creds:
                    try:
                        with open(TOKEN_FILE, 'w') as token:
                            token.write(self.creds.to_json())
                    except Exception as e:
                        logging.error(f"Hiba a token mentésekor: {e}")

            if self.creds:
                self.service = build('drive', 'v3', credentials=self.creds)
                self.authenticated = True
                self._update_status("Sikeresen bejelentkezve.", authenticated=True)
                return self.service

            self.authenticated = False
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return None

    def authenticate_google_drive(self):
        logging.info("Google Drive hitelesítés indítása...")
        self._update_status("Hitelesítés folyamatban...", authenticated=False)
        threading.Thread(target=self._authenticate_thread, daemon=True).start()

    def _authenticate_thread(self):
        service = self._get_drive_service()
        if service:
            self._update_status("Sikeresen bejelentkezve.", authenticated=True)
            logging.info("Google Drive hitelesítés sikeres.")
        else:
            self._update_status("Hitelesítés sikertelen.", authenticated=False)
            logging.error("Google Drive hitelesítés sikertelen.")

    def sign_out_google_drive(self):
        if not DRIVE_API_AVAILABLE:
            self._update_status("Google Drive API modulok hiányoznak.", authenticated=False)
            return

        with self.lock:
            self.creds = None
            self.service = None
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            self.authenticated = False
            self.last_backup_time = None
            self._update_status("Kijelentkezve.", authenticated=False, last_backup=None)
            logging.info("Google Drive kijelentkezés sikeres.")

    def _find_or_create_folder(self, service):
        # Keresés meglévő mappa után
        query = f"name='{DRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = response.get('files', [])
        if files:
            logging.info(f"Meglévő Google Drive mappa található: {files[0]['name']} (ID: {files[0]['id']})")
            return files[0]['id']
        else:
            # Ha nem található, létrehozás
            file_metadata = {
                'name': DRIVE_FOLDER_NAME,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            folder = service.files().create(body=file_metadata, fields='id').execute()
            logging.info(f"Google Drive mappa létrehozva: {DRIVE_FOLDER_NAME} (ID: {folder.get('id')})")
            return folder.get('id')

    def list_drive_files(self):
        if not self.authenticated:
            self.authenticate_google_drive() # Megpróbáljuk hitelesíteni, ha nincs bejelentkezve
            return # A listázás a hitelesítés után fog lefutni

        logging.info("Google Drive fájlok listázása indult...")
        self._update_status("Fájlok lekérése...", authenticated=True)
        threading.Thread(target=self._list_drive_files_thread, daemon=True).start()

    def _list_drive_files_thread(self):
        service = self._get_drive_service()
        if not service:
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        try:
            folder_id = self._find_or_create_folder(service)
            if not folder_id:
                self._update_status("Mappa létrehozása/keresése sikertelen.", authenticated=True)
                return

            query = f"'{folder_id}' in parents and trashed=false"
            response = service.files().list(q=query, spaces='drive', fields='files(id, name, modifiedTime, size)').execute()
            files = response.get('files', [])
            logging.info(f"Google Drive fájlok lekérve. Találatok: {len(files)}")
            self._update_status(f"Fájlok lekérve. ({len(files)} találat)", authenticated=True)
            wx.CallAfter(self.main_frame.settings_panel.update_drive_file_list, files)

        except Exception as e:
            logging.error(f"Hiba a Google Drive fájlok listázásakor: {e}")
            self._update_status(f"Hiba a fájlok listázásakor: {e}", authenticated=True)

    def upload_file_to_drive(self, local_file_path):
        if not self.authenticated:
            logging.warning("Nincs bejelentkezve a Google Drive-ba. Fájl feltöltés sikertelen.")
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        logging.info(f"Fájl feltöltése Google Drive-ra indult: {local_file_path}")
        self._update_status(f"Feltöltés folyamatban: {os.path.basename(local_file_path)}...", authenticated=True)
        threading.Thread(target=self._upload_file_to_drive_thread, args=(local_file_path,), daemon=True).start()

    def _upload_file_to_drive_thread(self, local_file_path):
        service = self._get_drive_service()
        if not service:
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        try:
            folder_id = self._find_or_create_folder(service)
            if not folder_id:
                self._update_status("Mappa létrehozása/keresése sikertelen.", authenticated=True)
                return

            file_name = os.path.basename(local_file_path)

            # Ellenőrizzük, hogy létezik-e már ilyen nevű fájl a mappában
            query = f"name='{file_name}' and '{folder_id}' in parents and trashed=false"
            response = service.files().list(q=query, spaces='drive', fields='files(id)').execute()
            existing_files = response.get('files', [])

            file_metadata = {
                'name': file_name,
                'parents': [folder_id]
            }
            media = MediaFileUpload(local_file_path, resumable=True)

            if existing_files:
                # Frissítjük a meglévő fájlt
                file_id = existing_files[0]['id']
                service.files().update(fileId=file_id, media_body=media).execute()
                logging.info(f"Google Drive fájl frissítve: {file_name}")
                self._update_status(f"Fájl frissítve: {file_name}", authenticated=True, last_backup=datetime.datetime.now())
            else:
                # Létrehozzuk az új fájlt
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                logging.info(f"Google Drive fájl feltöltve: {file_name}")
                self._update_status(f"Fájl feltöltve: {file_name}", authenticated=True, last_backup=datetime.datetime.now())

        except Exception as e:
            logging.error(f"Hiba a Google Drive feltöltés során: {e}")
            self._update_status(f"Feltöltési hiba: {e}", authenticated=True)

    def download_file_from_drive(self, file_id, file_name, local_path):
        if not self.authenticated:
            logging.warning("Nincs bejelentkezve a Google Drive-ba. Fájl letöltés sikertelen.")
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        logging.info(f"Fájl letöltése Google Drive-ról indult: {file_name} (ID: {file_id})")
        self._update_status(f"Letöltés folyamatban: {file_name}...", authenticated=True)
        threading.Thread(target=self._download_file_from_drive_thread, args=(file_id, file_name, local_path), daemon=True).start()

    def _download_file_from_drive_thread(self, file_id, file_name, local_path):
        service = self._get_drive_service()
        if not service:
            self._update_status("Nincs bejelentkezve.", authenticated=False)
            return

        try:
            request = service.files().get_media(fileId=file_id)
            with open(local_path, 'wb') as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while done is False:
                    status, done = downloader.next_chunk()
            logging.info(f"Google Drive fájl letöltve: {local_path}")
            self._update_status(f"Fájl letöltve: {file_name}", authenticated=True)
            # Sikeres letöltés után töltse be a schedule-t vagy settings-t
            if file_name == SCHEDULE_FILE:
                wx.CallAfter(self.main_frame.load_bell_schedule)
            elif file_name == SETTINGS_FILE:
                wx.CallAfter(self.main_frame.load_settings)
        except Exception as e:
            logging.error(f"Hiba a Google Drive letöltés során: {e}")
            self._update_status(f"Letöltési hiba: {e}", authenticated=True)

class SettingsManager:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.settings_file = SETTINGS_FILE
        self.settings = self.load_settings()

    def load_settings(self):
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    logging.info("Beállítások betöltve.")
                    return settings
            except Exception as e:
                logging.error(f"Hiba a beállítások betöltésekor: {e}")
        # Alapértelmezett értékek
        return {
            'volume': 50,
            'check_interval': 5.0,
            'ducking_enabled': False # Új beállítás
        }

    def save_settings(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4)
            logging.info("Beállítások elmentve.")
            self.main_frame.show_status_message("Beállítások elmentve.")
            # Feltöltés Google Drive-ra is, ha be van jelentkezve
            if self.main_frame.drive_manager.authenticated:
                self.main_frame.drive_manager.upload_file_to_drive(self.settings_file)
        except Exception as e:
            logging.error(f"Hiba a beállítások mentésekor: {e}")
            self.main_frame.show_status_message(f"Hiba a beállítások mentésekor: {e}")

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value
        self.save_settings()

class BellScheduleManager:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.schedule_file = SCHEDULE_FILE
        self.bell_schedule = self.load_bell_schedule()

    def load_bell_schedule(self):
        if os.path.exists(self.schedule_file):
            try:
                with open(self.schedule_file, 'r', encoding='utf-8') as f:
                    schedule = json.load(f)
                    logging.info("Ütemezés betöltve: csengetesi_rend.json")
                    self.main_frame.show_status_message("Ütemezés betöltve: csengetesi_rend.json")
                    return schedule
            except Exception as e:
                logging.error(f"Hiba az csengetési rend betöltésekor: {e}")
        logging.info("Nincs meglévő csengetési rend fájl, üres lista indul.")
        return []

    def save_bell_schedule(self):
        try:
            # Rendezzük az csengetéseket idő szerint mentés előtt
            self.bell_schedule.sort(key=lambda x: datetime.datetime.strptime(x['time'], "%H:%M").time())
            with open(self.schedule_file, 'w', encoding='utf-8') as f:
                json.dump(self.bell_schedule, f, indent=4)
            logging.info("Csengetési rend elmentve.")
            self.main_frame.show_status_message("Csengetési rend elmentve.")
            # Feltöltés Google Drive-ra is, ha be van jelentkezve
            if self.main_frame.drive_manager.authenticated:
                self.main_frame.drive_manager.upload_file_to_drive(self.schedule_file)
        except Exception as e:
            logging.error(f"Hiba az csengetési rend mentésekor: {e}")
            self.main_frame.show_status_message(f"Hiba az csengetési rend mentésekor: {e}")

    def add_bell(self, bell_data):
        self.bell_schedule.append(bell_data)
        logging.info(f"Új csengetés hozzáadva: {bell_data['time']}")
        self.save_bell_schedule()

    def update_bell(self, index, new_bell_data):
        if 0 <= index < len(self.bell_schedule):
            self.bell_schedule[index] = new_bell_data
            logging.info(f"Csengetés frissítve (index: {index}): {new_bell_data['time']}")
            self.save_bell_schedule()
            return True
        return False

    def delete_bell(self, index):
        if 0 <= index < len(self.bell_schedule):
            deleted_bell = self.bell_schedule.pop(index)
            logging.info(f"Csengetés törölve (index: {index}): {deleted_bell['time']}")
            self.save_bell_schedule()
            return True
        return False

    def get_bell_by_index(self, index):
        if 0 <= index < len(self.bell_schedule):
            return copy.deepcopy(self.bell_schedule[index])
        return None

    def get_bells_for_day(self, day):
        # Ha "Összes nap" van kiválasztva, visszaadjuk az összes csengetést
        if day == "Összes nap":
            return self.bell_schedule
        return [bell for bell in self.bell_schedule if day in bell.get('weekdays', [])]

    def copy_bells_to_days(self, source_day, destination_days):
        if source_day not in WEEKDAYS_HUNGARIAN:
            logging.error(f"Érvénytelen forrás nap: {source_day}")
            return

        bells_to_copy = self.get_bells_for_day(source_day)
        added_count = 0
        skipped_count = 0

        for dest_day in destination_days:
            if dest_day not in WEEKDAYS_HUNGARIAN:
                logging.warning(f"Érvénytelen cél nap kihagyva: {dest_day}")
                continue
            if dest_day == source_day:
                logging.info(f"Forrás és cél nap megegyezik ({dest_day}), kihagyva a másolást ide.")
                continue

            for bell in bells_to_copy:
                # Létrehozunk egy mély másolatot az csengetésről
                new_bell = copy.deepcopy(bell)

                # Frissítjük a napokat az új napra
                new_bell['weekdays'] = [dest_day]

                # Ellenőrizzük, hogy létezik-e már pontosan ilyen csengetés a cél napon
                # (idő, hangfájl és név egyezik az adott napra)
                already_exists = False
                for existing_bell in self.get_bells_for_day(dest_day):
                    if (existing_bell['time'] == new_bell['time'] and
                        existing_bell.get('name') == new_bell.get('name') and
                        existing_bell.get('sound_file') == new_bell.get('sound_file')):
                        already_exists = True
                        break

                if not already_exists:
                    self.bell_schedule.append(new_bell)
                    added_count += 1
                    logging.info(f"Csengetés másolva: {new_bell['time']} - {new_bell.get('name')} ide: {dest_day}")
                else:
                    skipped_count += 1
                    logging.info(f"Duplikátum csengetés kihagyva: {new_bell['time']} - {new_bell.get('name')} ide: {dest_day}")

        if added_count > 0:
            self.save_bell_schedule()
            wx.PostEvent(self.main_frame, ScheduleUpdatedEvent()) # Frissítjük a UI-t
            self.main_frame.show_status_message(f"{added_count} csengetés másolva. {skipped_count} kihagyva.")
            logging.info(f"Sikeresen másolva {added_count} csengetés, {skipped_count} duplikátum kihagyva.")
        else:
            self.main_frame.show_status_message("Nincs új csengetés másolva.")
            logging.info("Nincs új csengetés másolva.")


class BellPlayer:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.is_playing = False
        self.stop_requested = False
        self.current_sound_file = None
        self.player_thread = None

        try:
            pygame.mixer.init()
            logging.info("Pygame mixer inicializálva.")
        except Exception as e:
            logging.error(f"Hiba a Pygame mixer inicializálásakor: {e}")

    def play_sound(self, sound_file, volume):
        if not pygame.mixer.get_init():
            logging.error("Pygame mixer nincs inicializálva. A hang lejátszása sikertelen.")
            self.main_frame.show_status_message("Hiba: Hang lejátszás nem lehetséges (mixer hiba).")
            return

        if self.is_playing:
            self.stop_sound()
            time.sleep(0.1) # Várjunk egy kicsit, hogy a stop végrehajtódjon

        self.stop_requested = False
        self.current_sound_file = sound_file

        self.player_thread = threading.Thread(target=self._play_sound_thread, args=(sound_file, volume), daemon=True)
        self.player_thread.start()

    def _play_sound_thread(self, sound_file, volume):
        if not os.path.exists(sound_file):
            logging.error(f"A hangfájl nem található: {sound_file}")
            wx.CallAfter(self.main_frame.show_status_message, f"Hiba: A hangfájl nem található: {os.path.basename(sound_file)}")
            self.is_playing = False
            return

        try:
            pygame.mixer.music.load(sound_file)
            pygame.mixer.music.set_volume(volume / 100.0)
            pygame.mixer.music.play()
            self.is_playing = True
            logging.info(f"Hang lejátszása indult: {sound_file}, hangerő: {volume}")
            wx.CallAfter(self.main_frame.show_status_message, f"Csengetés szól: {os.path.basename(sound_file)}")

            while pygame.mixer.music.get_busy() and not self.stop_requested:
                time.sleep(0.1)

            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
            self.is_playing = False
            self.current_sound_file = None
            logging.info("Hang lejátszás befejeződött.")
            wx.CallAfter(self.main_frame.show_status_message, "Csengetés befejeződött.")
            wx.PostEvent(self.main_frame, BellFinishedPlayingEvent())

        except pygame.error as e:
            logging.error(f"Hiba a Pygame hang lejátszásakor: {e}")
            wx.CallAfter(self.main_frame.show_status_message, f"Hiba a hang lejátszásakor: {e}")
            self.is_playing = False
        except Exception as e:
            logging.error(f"Ismeretlen hiba a hang lejátszásakor: {e}")
            wx.CallAfter(self.main_frame.show_status_message, f"Ismeretlen hiba a hang lejátszásakor: {e}")
            self.is_playing = False


    def stop_sound(self):
        if self.is_playing:
            self.stop_requested = True
            logging.info("Hang lejátszás leállítási kérelem elküldve.")
            # Nem hívjuk meg itt a stop() és unload() fv-t,
            # mert a lejátszó szál felelős ezek végrehajtásáért.
            # Ehelyett várunk, hogy a szál befejeződjön.
            if self.player_thread and self.player_thread.is_alive():
                self.player_thread.join(timeout=1) # Max 1 mp-et várunk
                if self.player_thread.is_alive():
                    logging.warning("Lejátszó szál nem állt le időben.")
            self.is_playing = False
            self.current_sound_file = None
            logging.info("Hang lejátszás leállítva.")
            wx.CallAfter(self.main_frame.show_status_message, "Csengetés leállítva.")

class BellChecker:
    def __init__(self, main_frame, bell_player, bell_schedule_manager, settings_manager):
        self.main_frame = main_frame
        self.bell_player = bell_player
        self.bell_schedule_manager = bell_schedule_manager
        self.settings_manager = settings_manager
        self.timer = wx.Timer(main_frame)
        self.check_interval = self.settings_manager.get_setting('check_interval', 5.0)
        self.is_running = False
        self.thread = None
        self.stop_event = threading.Event()

    def start_checking(self):
        if self.is_running:
            return

        self.stop_event.clear()
        self.thread = threading.Thread(target=self._check_bells_thread, daemon=True)
        self.thread.start()
        self.is_running = True
        logging.info(f"Időzítő elindítva, ellenőrzési intervallum: {self.check_interval} másodperc.")
        logging.info("Ébresztő ellenőrző szál elindítva.")

    def stop_checking(self):
        if not self.is_running:
            return

        self.stop_event.set()
        logging.info("Ébresztő ellenőrző szál leállítási kérelem elküldve.")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=self.check_interval + 1) # Várjuk meg a szál leállását
            if self.thread.is_alive():
                logging.warning("Ébresztő ellenőrző szál nem állt le időben.")
        self.is_running = False
        logging.info("Ébresztő ellenőrző szál leállítva.")


    def _check_bells_thread(self):
        while not self.stop_event.is_set():
            now = datetime.datetime.now()
            current_time_str = now.strftime("%H:%M")
            current_weekday_index = now.weekday() # Hétfő=0, Vasárnap=6

            # Frissítsük a bell_schedule-t minden ellenőrzés előtt,
            # ha változott a fájl vagy a memória
            bell_schedule_copy = copy.deepcopy(self.bell_schedule_manager.bell_schedule)

            for bell in bell_schedule_copy:
                bell_time = bell['time']
                bell_weekdays = bell.get('weekdays', [])
                bell_name = bell.get('name', 'Névtelen csengetés')
                bell_sound_file = bell.get('sound_file')
                bell_volume = bell.get('volume', 50)
                bell_enabled = bell.get('enabled', True) # Alapértelmezett, hogy engedélyezve van

                if not bell_enabled:
                    continue # Kihagyjuk a letiltott csengetéseket

                # Ellenőrizzük a napokat
                if bell_weekdays and WEEKDAYS_HUNGARIAN[current_weekday_index] not in bell_weekdays:
                    continue

                if current_time_str == bell_time:
                    logging.info(f"Ébresztő szól: {bell_name} - {bell_time}")
                    if bell_sound_file:
                        full_sound_path = os.path.join('hangok', bell_sound_file) # Teljes elérési út
                        self.bell_player.play_sound(full_sound_path, bell_volume)
                    else:
                        wx.CallAfter(self.main_frame.show_status_message, f"Ébresztő szól: {bell_name} - {bell_time} (Nincs hangfájl beállítva)")
                    # Hogy ne szólaljon meg újra azonnal:
                    time.sleep(61) # Vár egy percet, mielőtt újra ellenőriz

            self.stop_event.wait(self.check_interval) # Vár a beállított intervallumot, vagy amíg meg nem állítják

    def update_check_interval(self, new_interval):
        self.check_interval = new_interval
        if self.is_running:
            self.stop_checking()
            self.start_checking()
        logging.info(f"Időzítő ellenőrzési intervallum frissítve: {new_interval} másodperc.")


class BellScheduleDialog(wx.Dialog):
    def __init__(self, parent, bell_data=None, available_sounds=None):
        super(BellScheduleDialog, self).__init__(parent, title="Csengetés hozzáadása", size=(400, 450))

        self.panel = wx.Panel(self)
        self.bell_data = bell_data if bell_data else {}
        self.available_sounds = available_sounds if available_sounds else []

        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Idő
        time_sizer = wx.BoxSizer(wx.HORIZONTAL)
        time_sizer.Add(wx.StaticText(self.panel, label="Idő:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        # Óra és perc kiválasztása külön legördülőkből
        time_sizer = wx.BoxSizer(wx.HORIZONTAL)
        time_sizer.Add(wx.StaticText(self.panel, label="Óra:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.hour_choice = wx.Choice(self.panel, choices=[f"{h:02d}" for h in range(24)])
        time_sizer.Add(self.hour_choice, 0, wx.ALL, 5)
        time_sizer.Add(wx.StaticText(self.panel, label="Perc:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.minute_choice = wx.Choice(self.panel, choices=[f"{m:02d}" for m in range(60)])
        time_sizer.Add(self.minute_choice, 0, wx.ALL, 5)
        main_sizer.Add(time_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # Név

        # Hangerő
        volume_sizer = wx.BoxSizer(wx.HORIZONTAL)
        volume_sizer.Add(wx.StaticText(self.panel, label="Hangerő:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.volume_slider = wx.Slider(self.panel, value=self.bell_data.get('volume', 50), minValue=0, maxValue=100,
                                      style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        volume_sizer.Add(self.volume_slider, 1, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(volume_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # Hangfájl
        sound_sizer = wx.BoxSizer(wx.HORIZONTAL)
        sound_sizer.Add(wx.StaticText(self.panel, label="Hangfájl:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.sound_choice = wx.Choice(self.panel, choices=self.available_sounds)
        sound_sizer.Add(self.sound_choice, 1, wx.EXPAND | wx.ALL, 5)
        test_sound_btn = wx.Button(self.panel, label="Teszt")
        test_sound_btn.Bind(wx.EVT_BUTTON, self.on_test_sound)
        sound_sizer.Add(test_sound_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
        main_sizer.Add(sound_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # Napok kiválasztása
        days_label = wx.StaticText(self.panel, label="Napok:")
        main_sizer.Add(days_label, 0, wx.ALL, 5)

        grid_sizer = wx.GridSizer(4, 2, 5, 5) # 4 sor, 2 oszlop, 5px vert és horiz. távolság
        self.day_checkboxes = {}
        for day in WEEKDAYS_HUNGARIAN:
            checkbox = wx.CheckBox(self.panel, label=day)
            grid_sizer.Add(checkbox, 0, wx.EXPAND)
            self.day_checkboxes[day] = checkbox
        main_sizer.Add(grid_sizer, 1, wx.EXPAND | wx.ALL, 5)

        # Gombok
        button_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(self.panel, wx.ID_OK, label="Mentés") # Label hozzáadva
        cancel_btn = wx.Button(self.panel, wx.ID_CANCEL)
        button_sizer.AddButton(ok_btn)
        button_sizer.AddButton(cancel_btn)
        button_sizer.Realize()

        main_sizer.Add(button_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        self.panel.SetSizer(main_sizer)
        self.panel.Layout() # Elrendezés frissítése

        if bell_data:
            self._load_bell_data(bell_data)
        else:
            if available_sounds:
                self.sound_choice.SetSelection(0) # Válassza ki az első hangot alapértelmezettként


    def _load_bell_data(self, bell_data):
        time_obj = datetime.datetime.strptime(bell_data['time'], "%H:%M").time()
        self.time_picker.SetValue(wx.DateTime.FromTime(time_obj.hour, time_obj.minute, 0))

        self.name_text.SetValue(bell_data.get('name', ''))
        self.volume_slider.SetValue(bell_data.get('volume', 50))

        try:
            index = self.sound_choice.FindString(bell_data.get('sound_file', ''))
            if index != wx.NOT_FOUND:
                self.sound_choice.SetSelection(index)
            else:
                wx.MessageBox(f"A korábbi hangfájl ({bell_data['sound_file']}) nem található. Kérjük válasszon újat.", "Hiányzó hangfájl", wx.OK | wx.ICON_WARNING)
        except Exception as e:
            logging.error(f"Hiba a hangfájl kiválasztásakor a dialógusban: {e}")
            # Napok beállítása
        selected_weekdays = bell_data.get('weekdays', [])
        for day, checkbox in self.day_checkboxes.items():
            checkbox.SetValue(day in selected_weekdays)

    def on_test_sound(self, event):
        selected_sound_index = self.sound_choice.GetSelection()
        if selected_sound_index == wx.NOT_FOUND:
            wx.MessageBox("Kérjük válasszon hangfájlt a teszteléshez.", "Nincs kijelölés", wx.OK | wx.ICON_WARNING)
            return
        sound_file = self.sound_choice.GetString(selected_sound_index)
        volume = self.volume_slider.GetValue()
        full_sound_path = os.path.join('hangok', sound_file) # Feltételezi, hogy a hangok mappában vannak
        # Használjuk a BellPlayer példányt a lejátszáshoz
        # Itt a self.GetParent() a BellSchedulePanel, annak a main_frame attribútuma a MainFrame
        # A BellPlayer pedig a MainFrame-hez tartozik.
        self.GetParent().main_frame.bell_player.play_sound(full_sound_path, volume)

    def GetBellData(self):
        # A validációt a Validate metódusban végezzük el
        hour = self.hour_choice.GetString(self.hour_choice.GetSelection())
        minute = self.minute_choice.GetString(self.minute_choice.GetSelection())
        time_str = f"{hour}:{minute}"
        name = time_str # Név helyett az időt használjuk
        volume = self.volume_slider.GetValue()
        sound_file = self.sound_choice.GetString(self.sound_choice.GetSelection())
        selected_weekdays = [day for day, checkbox in self.day_checkboxes.items() if checkbox.GetValue()]
        # Megtartjuk az enabled állapotot, ha szerkesztésről van szó
        enabled = self.bell_data.get('enabled', True)
        return {
            'time': time_str,
            'name': time_str,
            'sound_file': sound_file,
            'volume': volume,
            'weekdays': selected_weekdays,
            'enabled': enabled
        }

    # Hozzáadjuk ezt a metódust a dialógus bezárása előtt történő validációhoz
    def Validate(self):
        if self.sound_choice.GetSelection() == wx.NOT_FOUND:
            wx.MessageBox("Kérjük válasszon hangfájlt a csengetéshez.", "Hiányzó adat", wx.OK | wx.ICON_WARNING)
            self.sound_choice.SetFocus() # Fókuszáljunk a problémás mezőre
            return False
        if not self.day_checkboxes or not any(cb.GetValue() for cb in self.day_checkboxes.values()):
            wx.MessageBox("Kérjük válasszon legalább egy napot a csengetéshez.", "Hiányzó adat", wx.OK | wx.ICON_WARNING)
            return False
        return True

class CopyScheduleDialog(wx.Dialog):
    def __init__(self, parent):
        super(CopyScheduleDialog, self).__init__(parent, title="Csengetési rend másolása", size=(350, 400))
        self.panel = wx.Panel(self)
        self.selected_source_day = None
        self.selected_destination_days = []
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        # Forrás nap kiválasztása
        source_day_sizer = wx.BoxSizer(wx.HORIZONTAL)
        source_day_sizer.Add(wx.StaticText(self.panel, label="Forrás nap:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.source_day_choice = wx.Choice(self.panel, choices=WEEKDAYS_HUNGARIAN)
        source_day_sizer.Add(self.source_day_choice, 1, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(source_day_sizer, 0, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(wx.StaticText(self.panel, label="Cél nap(ok):"), 0, wx.ALL, 5)
        # Cél napok kiválasztása (checkboxok)
        grid_sizer = wx.GridSizer(4, 2, 5, 5)
        self.destination_day_checkboxes = {}
        for day in WEEKDAYS_HUNGARIAN:
            checkbox = wx.CheckBox(self.panel, label=day)
            grid_sizer.Add(checkbox, 0, wx.EXPAND)
            self.destination_day_checkboxes[day] = checkbox
        main_sizer.Add(grid_sizer, 1, wx.EXPAND | wx.ALL, 5)
        # Gombok
        button_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(self.panel, wx.ID_OK, label="Másolás")
        cancel_btn = wx.Button(self.panel, wx.ID_CANCEL)
        button_sizer.AddButton(ok_btn)
        button_sizer.AddButton(cancel_btn)
        button_sizer.Realize()
        main_sizer.Add(button_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)
        self.panel.SetSizer(main_sizer)
        self.panel.Layout()
        self.source_day_choice.SetSelection(0) # Alapértelmezett választás
    def GetCopyData(self):
        self.selected_source_day = self.source_day_choice.GetStringSelection()
        self.selected_destination_days = [
            day for day, checkbox in self.destination_day_checkboxes.items()
            if checkbox.GetValue()
        ]
        return self.selected_source_day, self.selected_destination_days

    def Validate(self):
        if self.source_day_choice.GetSelection() == wx.NOT_FOUND:
            wx.MessageBox("Kérjük válasszon ki egy forrás napot.", "Hiányzó adat", wx.OK | wx.ICON_WARNING)
            return False
        if not self.selected_destination_days:
            wx.MessageBox("Kérjük válasszon ki legalább egy cél napot.", "Hiányzó adat", wx.OK | wx.ICON_WARNING)
            return False
        return True

class SettingsPanel(wx.Panel):
    def __init__(self, parent, settings_manager, drive_manager):
        super(SettingsPanel, self).__init__(parent)
        self.settings_manager = settings_manager
        self.drive_manager = drive_manager
        self.main_frame = parent.main_frame

        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # --- Alkalmazás beállítások ---
        settings_box = wx.StaticBoxSizer(wx.StaticBox(self, label="Alkalmazás beállítások"), wx.VERTICAL)

        # Ellenőrzési intervallum
        interval_sizer = wx.BoxSizer(wx.HORIZONTAL)
        interval_sizer.Add(wx.StaticText(self, label="Ellenőrzési időköz (mp):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.interval_ctrl = wx.TextCtrl(self, value=str(self.settings_manager.get_setting('check_interval')), size=(50, -1))
        interval_sizer.Add(self.interval_ctrl, 0, wx.ALL, 5)
        self.interval_ctrl.Bind(wx.EVT_TEXT, self.on_interval_change)
        settings_box.Add(interval_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        # Ducking kapcsoló hozzáadása
        self.ducker_checkbox = wx.CheckBox(self, label="Ducking engedélyezése")
        self.ducker_checkbox.SetValue(self.settings_manager.get_setting('ducking_enabled', False))
        self.ducker_checkbox.Bind(wx.EVT_CHECKBOX, self.on_ducking_toggle)
        settings_box.Add(self.ducker_checkbox, 0, wx.ALL, 5)

        main_sizer.Add(settings_box, 0, wx.EXPAND | wx.ALL, 10)

        # --- Google Drive beállítások ---
        if DRIVE_API_AVAILABLE:
            drive_box = wx.StaticBoxSizer(wx.StaticBox(self, label="Google Drive biztonsági mentés"), wx.VERTICAL)

            # Drive állapot
            status_sizer = wx.BoxSizer(wx.HORIZONTAL)
            status_sizer.Add(wx.StaticText(self, label="Állapot:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
            self.drive_status_label = wx.StaticText(self, label="Nincs bejelentkezve.")
            self.drive_status_label.SetForegroundColour(wx.Colour(255, 0, 0))
            status_sizer.Add(self.drive_status_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
            drive_box.Add(status_sizer, 0, wx.EXPAND | wx.ALL, 5)

            # Gombok
            drive_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
            self.login_btn = wx.Button(self, label="Bejelentkezés")
            self.login_btn.Bind(wx.EVT_BUTTON, self.on_login_drive)
            self.logout_btn = wx.Button(self, label="Kijelentkezés")
            self.logout_btn.Bind(wx.EVT_BUTTON, self.on_logout_drive)
            self.list_files_btn = wx.Button(self, label="Fájlok listázása")
            self.list_files_btn.Bind(wx.EVT_BUTTON, self.on_list_drive_files)
            drive_btn_sizer.Add(self.login_btn, 0, wx.ALL, 5)
            drive_btn_sizer.Add(self.logout_btn, 0, wx.ALL, 5)
            drive_btn_sizer.Add(self.list_files_btn, 0, wx.ALL, 5)
            drive_box.Add(drive_btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 5)

            # Drive fájl lista
            self.file_list_ctrl = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
            self.file_list_ctrl.InsertColumn(0, 'Fájlnév', width=200)
            self.file_list_ctrl.InsertColumn(1, 'Dátum', width=150)
            self.file_list_ctrl.InsertColumn(2, 'Méret (KB)', width=100)
            self.file_list_ctrl.InsertColumn(3, 'ID', width=0) # Rejtett oszlop
            
            download_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
            self.download_btn = wx.Button(self, label="Kiválasztott letöltése")
            self.download_btn.Bind(wx.EVT_BUTTON, self.on_download_drive_file)
            download_btn_sizer.Add(self.download_btn, 0, wx.ALL, 5)
            
            drive_box.Add(self.file_list_ctrl, 1, wx.EXPAND | wx.ALL, 5)
            drive_box.Add(download_btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 5)

            main_sizer.Add(drive_box, 1, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(main_sizer)
        self.Layout()

        # Kezdeti állapot frissítése
        self.update_drive_status_label("Nincs bejelentkezve.", False, None)


    def update_drive_status_label(self, message, authenticated, last_backup_time):
        if hasattr(self, 'drive_status_label'):
            if authenticated:
                status_text = f"Bejelentkezve. Utolsó mentés: {last_backup_time.strftime('%Y-%m-%d %H:%M:%S')}" if last_backup_time else "Bejelentkezve."
                self.drive_status_label.SetForegroundColour(wx.Colour(0, 128, 0)) # Zöld
            else:
                status_text = message
                self.drive_status_label.SetForegroundColour(wx.Colour(255, 0, 0)) # Piros
            self.drive_status_label.SetLabel(status_text)
            self.login_btn.Enable(not authenticated)
            self.logout_btn.Enable(authenticated)
            self.list_files_btn.Enable(authenticated)
            self.download_btn.Enable(authenticated)
            self.Layout()


    def on_interval_change(self, event):
        try:
            new_interval = float(self.interval_ctrl.GetValue())
            if new_interval <= 0:
                raise ValueError
            self.settings_manager.set_setting('check_interval', new_interval)
            self.main_frame.bell_checker.update_check_interval(new_interval)
        except ValueError:
            logging.error("Hibás ellenőrzési időköz formátum.")
            self.main_frame.show_status_message("Hiba: Az ellenőrzési időköznek egy pozitív számnak kell lennie.")


    def on_ducking_toggle(self, event):
        enabled = self.ducker_checkbox.GetValue()
        self.settings_manager.set_setting('ducking_enabled', enabled)
        self.main_frame.show_status_message(f"Ducking {'engedélyezve' if enabled else 'letiltva'}.")
        self.main_frame._toggle_ducker(enabled)

    def on_login_drive(self, event):
        self.drive_manager.authenticate_google_drive()

    def on_logout_drive(self, event):
        self.drive_manager.sign_out_google_drive()

    def on_list_drive_files(self, event):
        self.drive_manager.list_drive_files()

    def update_drive_file_list(self, files):
        self.file_list_ctrl.DeleteAllItems()
        self.file_list_ctrl.Show(len(files) > 0)
        self.download_btn.Enable(len(files) > 0)
        
        # Drive fájl ID-k tárolása a letöltéshez
        self.drive_file_ids = {}

        for i, file in enumerate(files):
            file_name = file['name']
            file_id = file['id']
            modified_time = datetime.datetime.fromisoformat(file['modifiedTime'].replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
            file_size_kb = round(int(file['size']) / 1024) if 'size' in file else 0
            
            self.file_list_ctrl.InsertItem(i, file_name)
            self.file_list_ctrl.SetItem(i, 1, modified_time)
            self.file_list_ctrl.SetItem(i, 2, f"{file_size_kb} KB")
            
            # Rejtett ID hozzárendelése a listaelemhez
            self.drive_file_ids[i] = {'id': file_id, 'name': file_name}
        
        self.Layout()


    def on_download_drive_file(self, event):
        selected_item_index = self.file_list_ctrl.GetFirstSelected()
        if selected_item_index == -1:
            wx.MessageBox("Kérjük válasszon ki egy fájlt a letöltéshez.", "Nincs kijelölés", wx.OK | wx.ICON_WARNING)
            return
            
        selected_file = self.drive_file_ids.get(selected_item_index)
        if selected_file:
            file_id = selected_file['id']
            file_name = selected_file['name']
            
            local_path = file_name
            
            # Kérdezzük meg a felhasználót, hogy felülírja-e a meglévő fájlt
            if os.path.exists(local_path):
                msg_dlg = wx.MessageDialog(self,
                                        f"A(z) '{file_name}' nevű fájl már létezik. Felülírja?",
                                        "Fájl már létezik",
                                        wx.YES_NO | wx.ICON_QUESTION)
                if msg_dlg.ShowModal() == wx.ID_NO:
                    return
            
            self.drive_manager.download_file_from_drive(file_id, file_name, local_path)


class MainFrame(wx.Frame):
    def __init__(self, *args, **kw):
        super(MainFrame, self).__init__(*args, **kw)
        self.SetTitle("Vekker - Csengetési rend kezelő")
        self.SetSize((800, 600))
        
        self.settings_manager = SettingsManager(self)
        self.schedule_manager = BellScheduleManager(self)
        self.drive_manager = GoogleDriveManager(self)
        self.bell_player = BellPlayer(self)
        self.bell_checker = BellChecker(self, self.bell_player, self.schedule_manager, self.settings_manager)

        # UI elemek
        self.panel = wx.Panel(self)
        self.notebook = wx.Notebook(self.panel)
        
        # Oldalak hozzáadása a jegyzethez
        self.schedule_panel = BellSchedulePanel(self.notebook, self)
        self.notebook.AddPage(self.schedule_panel, "Csengetési rend")
        
        self.settings_panel = SettingsPanel(self.notebook, self.settings_manager, self.drive_manager)
        self.notebook.AddPage(self.settings_panel, "Beállítások")
        
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_page_changed)

        # Állapotsáv
        self.statusbar = self.CreateStatusBar()
        self.show_status_message("Alkalmazás elindítva.")

        # Menü
        file_menu = wx.Menu()
        exit_item = file_menu.Append(wx.ID_EXIT, "Kilépés", "Kilépés az alkalmazásból")
        self.Bind(wx.EVT_MENU, self.on_close, exit_item)

        menu_bar = wx.MenuBar()
        menu_bar.Append(file_menu, "Fájl")
        self.SetMenuBar(menu_bar)
        
        # Fő Sizer
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)
        self.panel.SetSizer(main_sizer)
        
        # Ablak események
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(EVT_DRIVE_STATUS, self.on_drive_status_update)
        self.Bind(EVT_SCHEDULE_UPDATED, self.schedule_panel.on_schedule_updated)
        
        # Ducking inicializálás
        self.ducker = None
        self._toggle_ducker(self.settings_manager.get_setting('ducking_enabled', False))
        
        # Indítsuk el a csengetés ellenőrzést
        self.bell_checker.start_checking()


    def show_status_message(self, message):
        self.statusbar.SetStatusText(message)


    def load_bell_schedule(self):
        self.schedule_manager.load_bell_schedule()
        self.schedule_panel.refresh_schedule_list()


    def load_settings(self):
        self.settings_manager.load_settings()
        # Frissítjük a UI elemeket az új beállításokkal
        self.settings_panel.interval_ctrl.SetValue(str(self.settings_manager.get_setting('check_interval', 5.0)))
        self.show_status_message("Beállítások betöltve.")


    def on_drive_status_update(self, event):
        self.settings_panel.update_drive_status_label(event.message, event.authenticated, event.last_backup_time)

    def on_page_changed(self, event):
        old_page = event.GetOldSelection()
        new_page = event.GetSelection()
        old_page_text = self.notebook.GetPageText(old_page)
        new_page_text = self.notebook.GetPageText(new_page)
        logging.info(f"Fül váltva: {new_page_text}")

        # Irány meghatározása (egyszerűsített)
        if new_page > old_page:
            logging.info(f"Fül váltva jobbra: {new_page_text}")
        elif new_page < old_page:
            logging.info(f"Fül váltva balra: {new_page_text}")
        event.Skip()


    def on_close(self, event):
        try:
            if hasattr(self, 'ducker') and self.ducker:
                self.ducker.stop()
                logging.info('DuckerVAD leállítva a kilépéskor (hangerő visszaállítva).')
        except Exception:
            logging.exception('DuckerVAD leállítási hiba kilépéskor.')
        logging.info("Alkalmazás bezárása.")
        self.bell_player.stop_sound()
        self.bell_checker.stop_checking()
        self.Destroy()

    def _toggle_ducker(self, enabled):
        if enabled:
            if not self.ducker:
                self.ducker = AdaptiveVoiceDuckerVAD()
                self.ducker.start()
                logging.info("DuckerVAD elindítva.")
            else:
                logging.info("DuckerVAD már fut, nem indítjuk újra.")
        else:
            if self.ducker:
                self.ducker.stop()
                self.ducker = None
                logging.info("DuckerVAD leállítva.")
            else:
                logging.info("DuckerVAD már le van állítva.")

class BellSchedulePanel(wx.Panel):
    def __init__(self, parent, main_frame):
        super(BellSchedulePanel, self).__init__(parent)
        self.main_frame = main_frame
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Napok kiválasztása
        day_choice_sizer = wx.BoxSizer(wx.HORIZONTAL)
        day_choice_sizer.Add(wx.StaticText(self, label="Megjelenített nap:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.day_choice = wx.Choice(self, choices=["Összes nap"] + WEEKDAYS_HUNGARIAN)
        self.day_choice.SetSelection(0)
        self.day_choice.Bind(wx.EVT_CHOICE, self.on_day_change)
        day_choice_sizer.Add(self.day_choice, 1, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(day_choice_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # Csengetési lista
        self.schedule_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.schedule_list.InsertColumn(0, 'Idő', width=70)
        self.schedule_list.InsertColumn(1, 'Hangerő', width=70)
        self.schedule_list.InsertColumn(2, 'Hangfájl', width=150)
        self.schedule_list.InsertColumn(3, 'Nap(ok)', width=120)
        self.schedule_list.InsertColumn(4, 'Név', width=150)
        self.schedule_list.InsertColumn(5, 'Engedélyezve', width=100)
        main_sizer.Add(self.schedule_list, 1, wx.EXPAND | wx.ALL, 10)

        # Gombok
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.add_btn = wx.Button(self, label="Hozzáadás")
        self.edit_btn = wx.Button(self, label="Szerkesztés")
        self.delete_btn = wx.Button(self, label="Törlés")
        self.copy_btn = wx.Button(self, label="Nap másolása")
        button_sizer.Add(self.add_btn, 0, wx.ALL, 5)
        button_sizer.Add(self.edit_btn, 0, wx.ALL, 5)
        button_sizer.Add(self.delete_btn, 0, wx.ALL, 5)
        button_sizer.Add(self.copy_btn, 0, wx.ALL, 5)
        
        self.toggle_enabled_btn = wx.Button(self, label="Engedélyezés/Tiltás")
        button_sizer.Add(self.toggle_enabled_btn, 0, wx.ALL, 5)
        
        main_sizer.Add(button_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 5)
        
        self.SetSizer(main_sizer)

        # Eseménykezelők
        self.add_btn.Bind(wx.EVT_BUTTON, self.on_add_bell)
        self.edit_btn.Bind(wx.EVT_BUTTON, self.on_edit_bell)
        self.delete_btn.Bind(wx.EVT_BUTTON, self.on_delete_bell)
        self.copy_btn.Bind(wx.EVT_BUTTON, self.on_copy_schedule)
        self.toggle_enabled_btn.Bind(wx.EVT_BUTTON, self.on_toggle_enabled)
        
        # Lista elemre duplán kattintás
        self.schedule_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_edit_bell)

        # Kezdeti lista frissítés
        self.refresh_schedule_list()


    def refresh_schedule_list(self):
        self.schedule_list.DeleteAllItems()
        selected_day = self.day_choice.GetStringSelection()
        bells = self.main_frame.schedule_manager.get_bells_for_day(selected_day)
        
        for i, bell in enumerate(bells):
            index = self.schedule_list.InsertItem(i, bell['time'])
            self.schedule_list.SetItem(index, 1, str(bell['volume']))
            self.schedule_list.SetItem(index, 2, bell['sound_file'])
            self.schedule_list.SetItem(index, 3, ", ".join(bell['weekdays']))
            self.schedule_list.SetItem(index, 4, bell.get('name', 'Névtelen csengetés'))
            enabled_text = "Igen" if bell.get('enabled', True) else "Nem"
            self.schedule_list.SetItem(index, 5, enabled_text)
            
            # Index hozzárendelése az eredeti listához, mert a filterezés miatt eltérhet a listCtrl indexétől
            self.schedule_list.SetItemData(index, self.main_frame.schedule_manager.bell_schedule.index(bell))
            
            # Színezés, ha le van tiltva
            if not bell.get('enabled', True):
                self.schedule_list.SetItemBackgroundColour(index, wx.LIGHT_GREY)


    def on_day_change(self, event):
        self.refresh_schedule_list()

    def on_add_bell(self, event):
        available_sounds = self.get_available_sound_files()
        if not available_sounds:
            wx.MessageBox("Nincs hangfájl a 'hangok' mappában. Kérjük másoljon be hangokat.", "Hiányzó hangfájlok", wx.OK | wx.ICON_WARNING)
            return

        with BellScheduleDialog(self, available_sounds=available_sounds) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                if dlg.Validate():
                    bell_data = dlg.GetBellData()
                    self.main_frame.schedule_manager.add_bell(bell_data)
                    self.refresh_schedule_list()
                    self.main_frame.show_status_message("Új csengetés hozzáadva.")


    def on_edit_bell(self, event):
        selected_item_index = self.schedule_list.GetFirstSelected()
        if selected_item_index == -1:
            wx.MessageBox("Kérjük válasszon ki egy csengetést a szerkesztéshez.", "Nincs kijelölés", wx.OK | wx.ICON_WARNING)
            return
            
        original_index = self.schedule_list.GetItemData(selected_item_index)
        bell_data = self.main_frame.schedule_manager.get_bell_by_index(original_index)
        available_sounds = self.get_available_sound_files()

        if bell_data and available_sounds:
            with BellScheduleDialog(self, bell_data, available_sounds) as dlg:
                if dlg.ShowModal() == wx.ID_OK:
                    if dlg.Validate():
                        new_bell_data = dlg.GetBellData()
                        self.main_frame.schedule_manager.update_bell(original_index, new_bell_data)
                        self.refresh_schedule_list()
                        self.main_frame.show_status_message("Csengetés frissítve.")
        else:
            wx.MessageBox("Nem sikerült betölteni a csengetés adatait vagy nincsenek elérhető hangok.", "Hiba", wx.OK | wx.ICON_ERROR)


    def on_delete_bell(self, event):
        selected_item_index = self.schedule_list.GetFirstSelected()
        if selected_item_index == -1:
            wx.MessageBox("Kérjük válasszon ki egy csengetést a törléshez.", "Nincs kijelölés", wx.OK | wx.ICON_WARNING)
            return
            
        original_index = self.schedule_list.GetItemData(selected_item_index)
        
        confirm_dlg = wx.MessageDialog(self,
                                        "Biztosan törli a kiválasztott csengetést?",
                                        "Törlés megerősítése",
                                        wx.YES_NO | wx.ICON_QUESTION)
        
        if confirm_dlg.ShowModal() == wx.ID_YES:
            if self.main_frame.schedule_manager.delete_bell(original_index):
                self.refresh_schedule_list()
                self.main_frame.show_status_message("Csengetés törölve.")


    def on_toggle_enabled(self, event):
        selected_item_index = self.schedule_list.GetFirstSelected()
        if selected_item_index == -1:
            wx.MessageBox("Kérjük válasszon ki egy csengetést az állapot módosításához.", "Nincs kijelölés", wx.OK | wx.ICON_WARNING)
            return

        original_index = self.schedule_list.GetItemData(selected_item_index)
        bell_data = self.main_frame.schedule_manager.get_bell_by_index(original_index)
        
        if bell_data:
            bell_data['enabled'] = not bell_data.get('enabled', True)
            self.main_frame.schedule_manager.update_bell(original_index, bell_data)
            self.refresh_schedule_list()
            status_text = f"Csengetés {'engedélyezve' if bell_data['enabled'] else 'letiltva'}."
            self.main_frame.show_status_message(status_text)
        else:
            wx.MessageBox("Nem sikerült betölteni a csengetés adatait.", "Hiba", wx.OK | wx.ICON_ERROR)


    def on_copy_schedule(self, event):
        with CopyScheduleDialog(self) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                source_day, destination_days = dlg.GetCopyData()
                if dlg.Validate():
                    self.main_frame.schedule_manager.copy_bells_to_days(source_day, destination_days)


    def on_schedule_updated(self, event):
        self.refresh_schedule_list()


    def get_available_sound_files(self):
        sound_dir = 'hangok'
        if not os.path.exists(sound_dir):
            os.makedirs(sound_dir)
            logging.info(f"Létrehozva a(z) '{sound_dir}' mappa.")
            return []
        files = [f for f in os.listdir(sound_dir) if os.path.isfile(os.path.join(sound_dir, f))]
        return sorted(files)

# --- Az alkalmazás indítása ---
if __name__ == '__main__':
    app = wx.App()
    frame = MainFrame(None, title="Vekker")
    frame.Show()
    app.MainLoop()