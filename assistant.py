# я сделал голосового ассистента как у алисы только свой
# он открывает ютуб вк кино и стим, еще умеет болтать
# и может управлять громкостью жестами (рукой)
# чота не работало, я починил. теперь работает!!! (вроде)
#
# + пофиксил озвучку (движок пересоздаётся каждый раз)
# + ускорил открытие жестов (модель греется в фоне)
# + теперь пишет что я сказал
# + камера открывается в главном потоке (иначе окно не показывается)
# + сохраняю что я сказал в output.wav
# + окно жестов упростил: только процент громкости, точки и линия

import os
import sys
import math
import time
import random
import subprocess
import webbrowser
import threading

import numpy as np
import scipy.io.wavfile as wavfile

import speech_recognition as sr
import pyttsx3

# для громкости
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# для жестов
import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

# для сглаживания громкости
from collections import deque



#   ОЗВУЧКА (чтоб ассистент говорил)
class Govorilka:
    def __init__(self):
        self.voice_id = None
        self.est_russkiy = False

        # один раз ищу русский голос
        try:
            engine = pyttsx3.init()
            for v in engine.getProperty("voices"):
                name = (v.name or "").lower()
                vid = (v.id or "").lower()
                if ("russian" in name or "ru-" in vid
                        or "irina" in name or "pavel" in name):
                    self.voice_id = v.id
                    self.est_russkiy = True
                    print("Озвучка: нашёл русский голос:", v.name)
                    break
            if not self.est_russkiy:
                print("Озвучка: РУССКИЙ ГОЛОС НЕ НАЙДЕН!")
                print("Добавь его: Параметры -> Время и язык -> Речь")
            try:
                engine.stop()
            except Exception:
                pass
            del engine
        except Exception as e:
            print("Ошибка поиска голосов:", e)

    def skazat(self, text):
        print("Ассистент:", text)

        #
        # иначе pyttsx3 на винде часто зависает после 1й фразы
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 180)
            engine.setProperty("volume", 1.0)

            if self.voice_id:
                engine.setProperty("voice", self.voice_id)

            engine.say(text)
            engine.runAndWait()

            try:
                engine.stop()
            except Exception:
                pass
            del engine

        except Exception as e:
            print("ой, озвучка сломалась:", e)



#   ГРОМКОСТЬ ЧЕРЕЗ ЖЕСТЫ (рукой)
class RukaGromkost:
    def __init__(self, model_path="hand_landmarker.task"):
        if not os.path.exists(model_path):
            print("нету файла", model_path)
            print("скачай отсюда:")
            print("https://storage.googleapis.com/mediapipe-models/"
                  "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task")
            sys.exit(1)

        print("Жесты: загружаю модель...")
        base_options = mp_python.BaseOptions(model_asset_path=model_path)
        options = mp_vision.HandLandmarkerOptions(
            base_options=base_options,
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5
        )
        self.detector = mp_vision.HandLandmarker.create_from_options(options)
        self.timestamp = 0

        # камеру НЕ открываю тут - только в rabotat()
        self.cap = None

        # громкость винды
        devices = AudioUtilities.GetSpeakers()
        if hasattr(devices, "Activate"):
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        else:
            interface = devices._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self.volume = cast(interface, POINTER(IAudioEndpointVolume))

        # сглаживание
        self.buf = deque(maxlen=6)
        self.current_pct = self.volume.GetMasterVolumeLevelScalar() * 100.0
        self.last_time = 0

        # границы (если рука близко - тише, далеко - громче)
        self.blizko = 0.03
        self.daleko = 0.20

        print("Жесты: модель готова! громкость щас:", round(self.current_pct), "%")

    def ustanovit_gromkost(self, pct):
        if pct < 0:
            pct = 0
        if pct > 100:
            pct = 100
        self.volume.SetMasterVolumeLevelScalar(pct / 100.0, None)

    def rasstoyanie(self, tochki):
        x1, y1 = tochki[4][0], tochki[4][1]
        x2, y2 = tochki[8][0], tochki[8][1]
        return math.hypot(x2 - x1, y2 - y1)

    def risovat(self, frame, dist, vol, est_ruka):
        # просто пишу процент громкости большими буквами
        # если рука есть - зелёным, если нет - красным
        if est_ruka:
            cv2.putText(frame, "{:.0f}%".format(vol),
                        (20, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.8,
                        (0, 255, 0), 4)
        else:
            cv2.putText(frame, "{:.0f}%".format(vol),
                        (20, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.8,
                        (0, 0, 255), 4)

        return frame

    def rabotat(self):
        # ВАЖНО: камеру открываем ТУТ, в главном потоке,
        # иначе cv2.imshow не покажет окно
        print("Жесты: включаю камеру...")
        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            print("камера не открылась(")
            return

        # поменьше разрешение - быстрее работает
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)

        # прогреваю камеру - делаю пару холостых кадров
        for _ in range(3):
            self.cap.read()

        print("=" * 50)
        print("УПРАВЛЕНИЕ ГРОМКОСТЬЮ РУКОЙ")
        print("сведи пальцы - тише, разведи - громче")
        print("нажми Q чтобы выйти")
        print("=" * 50)

        while True:
            ok, frame = self.cap.read()
            if not ok:
                print("кадр не получился")
                break

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

            result = self.detector.detect_for_video(mp_img, self.timestamp)
            self.timestamp += 33

            est_ruka = False
            dist = 0.0
            vol = self.current_pct

            if result.hand_landmarks:
                est_ruka = True
                lm = result.hand_landmarks[0]
                tochki = [(t.x, t.y, t.z) for t in lm]

                dist = self.rasstoyanie(tochki)

                h, w, _ = frame.shape
                for idx in (4, 8):
                    cx = int(tochki[idx][0] * w)
                    cy = int(tochki[idx][1] * h)
                    cv2.circle(frame, (cx, cy), 8, (0, 255, 0), -1)

                x1 = int(tochki[4][0] * w); y1 = int(tochki[4][1] * h)
                x2 = int(tochki[8][0] * w); y2 = int(tochki[8][1] * h)
                cv2.line(frame, (x1, y1), (x2, y2), (255, 0, 255), 2)

                t = (dist - self.blizko) / (self.daleko - self.blizko)
                if t < 0:
                    t = 0
                if t > 1:
                    t = 1
                target = t * 100.0

                self.buf.append(target)
                smooth = sum(self.buf) / len(self.buf)

                now = time.time()
                if now - self.last_time >= 0.05:
                    if abs(smooth - self.current_pct) > 1.0:
                        self.ustanovit_gromkost(smooth)
                        self.current_pct = smooth
                    self.last_time = now

                vol = smooth

            frame = self.risovat(frame, dist, vol, est_ruka)
            cv2.imshow("Gromkost rukoy", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q") or key == ord("Q"):
                break

        cv2.destroyAllWindows()
        print("выхожу из режима жестов")

    def zakryt(self):
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass



#   СЛУШАЮ МИКРОФОН (тупо, без наворотов)
def slushat():
    r = sr.Recognizer()
    mic = sr.Microphone()

    # вот эти 3 строчки я подобрал чтобы лучше слышало
    r.energy_threshold = 3000
    r.pause_threshold = 0.8
    r.phrase_threshold = 0.3

    with mic as source:
        print("Слушаю...")
        try:
            audio = r.listen(source, timeout=5, phrase_time_limit=8)
        except sr.WaitTimeoutError:
            return None

    # вот тут сохраняю что я сказал в output.wav
    # (в ту же папку где скрипт)
    try:
        # из AudioData достаю сырые байты и делаю из них массив
        syrye = audio.get_raw_data(convert_rate=16000, convert_width=2)
        massiv = np.frombuffer(syrye, dtype=np.int16)
        wavfile.write("output.wav", 16000, massiv)
        print("сохранил запись в output.wav")
    except Exception as e:
        print("не смог сохранить wav:", e)

    try:
        text = r.recognize_google(audio, language="ru-RU")
        # вот тут показываю что я сказал
        print("Ты сказал:", text)
        return text
    except sr.UnknownValueError:
        print("не разобрал что ты сказал(")
        return None
    except sr.RequestError as e:
        print("интернет чота не работает:", e)
        return None



#   СЛОВАРЬ ОТВЕТОВ (чтоб болтал)
OTVETY = {
    ("привет", "здравствуй", "хай", "прив"): [
        "Привет! Чем помочь?",
        "Здравствуй! Слушаю.",
        "Привет-привет!",
    ],
    ("как дела", "как ты", "как жизнь", "как настроение"): [
        "Всё отлично! А у тебя как?",
        "Прекрасно! Как сам?",
        "Лучше всех!",
    ],
    ("что делаешь", "чем занят", "чем занимаешься"): [
        "Слушаю тебя и жду команды.",
        "Обрабатываю звук с микрофона.",
        "Стою жду указаний.",
    ],
    ("спасибо", "благодарю", "спс"): [
        "Всегда пожалуйста!",
        "Рад помочь!",
        "Обращайся!",
    ],
    ("пока", "до свидания", "увидимся"): [
        "Пока! Хорошего дня!",
        "До встречи!",
        "Увидимся!",
    ],
    ("ты кто", "как тебя зовут", "твое имя", "твоё имя"): [
        "Я твой голосовой ассистент.",
        "Я помощник, созданный тобой.",
    ],
    ("кто тебя создал", "кто тебя сделал"): [
        "Меня создал мой хозяин.",
        "Меня написал один программист.",
    ],
    ("что умеешь", "что ты можешь", "твои возможности"): [
        "Открываю ютуб, вк, кинопоиск, стим. И управляю громкостью жестами.",
    ],
    ("хорошо", "окей", "ок", "ладно"): [
        "Отлично!",
        "Принято.",
        "Хорошо.",
    ],
    ("плохо", "грустно"): [
        "Не грусти, всё наладится.",
        "Держись!",
    ],
}


def naydi_otvet(text):
    text = text.lower()
    for klyuchi, otvety in OTVETY.items():
        for k in klyuchi:
            if k in text:
                return random.choice(otvety)
    return None



#   ОТКРЫТЬ STEAM
def otkryt_steam():
    puti = [
        r"C:\Program Files (x86)\Steam\Steam.exe",
        r"C:\Program Files\Steam\Steam.exe",
    ]
    for p in puti:
        if os.path.exists(p):
            try:
                subprocess.Popen([p])
                return True
            except Exception as e:
                print("стим не запустился:", e)
                return False
    print("не нашел стим. проверь путь.")
    return False



#   ЧТО ДЕЛАТЬ С КОМАНДОЙ
def obrabotat(text, govorilka):
    niz = text.lower()

    # выход
    if "стоп" in niz or "выход" in niz:
        govorilka.skazat("Завершаю работу. До свидания!")
        return "exit"

    # жесты
    if "громкость" in niz or "громче" in niz or "звук" in niz:
        govorilka.skazat("Включаю управление громкостью жестами.")
        return "gestures"

    # сайты
    if "ютуб" in niz or "youtube" in niz:
        webbrowser.open_new_tab("https://youtube.com")
        govorilka.skazat("Открываю ютуб")
    elif "вк" in niz or "вконтакте" in niz:
        webbrowser.open_new_tab("https://vk.com")
        govorilka.skazat("Открываю в контакте")
    elif "кино" in niz or "кинопоиск" in niz:
        webbrowser.open_new_tab("https://hd.kinopoisk.ru/")
        govorilka.skazat("Открываю кинопоиск")
    elif "стим" in niz or "steam" in niz:
        if otkryt_steam():
            govorilka.skazat("Открываю стим")
        else:
            govorilka.skazat("Не нашёл стим")
    else:
        otvet = naydi_otvet(text)
        if otvet:
            govorilka.skazat(otvet)
        else:
            govorilka.skazat("Не понял. Повтори пожалуйста.")

    return None



#   ФОНОВАЯ ПОДГОТОВКА (грузим только модель)
ruka_global = None
ruka_gotova = threading.Event()
ruka_oshibka = None


def podgotovit_ruku_v_fone():
    """Грузим только модель заранее. Камера - в главном потоке."""
    global ruka_global, ruka_oshibka
    try:
        print("(фон) готовлю модель жестов заранее...")
        ruka_global = RukaGromkost()
        ruka_gotova.set()
        print("(фон) модель жестов готова!")
    except Exception as e:
        ruka_oshibka = e
        ruka_gotova.set()



#   ЗАПУСК ВСЕГО
def main():
    print("=" * 50)
    print("МОЙ ГОЛОСОВОЙ АССИСТЕНТ")
    print("=" * 50)
    print("команды: ютуб, вк, кино, стим, громкость, выход")
    print("а еще можно просто поболтать: привет, как дела, спасибо...")
    print("=" * 50)

    govorilka = Govorilka()

    # запускаю подготовку модели в фоне
    potok = threading.Thread(target=podgotovit_ruku_v_fone, daemon=True)
    potok.start()

    govorilka.skazat("Ассистент запущен. Слушаю тебя.")

    while True:
        text = slushat()
        if not text:
            continue

        deystvie = obrabotat(text, govorilka)

        if deystvie == "exit":
            break

        if deystvie == "gestures":
            # ждём, пока фоновая подготовка модели закончится
            if not ruka_gotova.is_set():
                print("подожди, модель ещё грузится...")
                govorilka.skazat("Секунду, готовлю жесты.")
                ruka_gotova.wait(timeout=15)

            if ruka_oshibka is not None:
                print("жесты не запустились:", ruka_oshibka)
                govorilka.skazat("Не получилось запустить жесты.")
            elif ruka_global is not None:
                # ВАЖНО: rabotat() вызывается в ГЛАВНОМ потоке -
                # камера и окно откроются нормально
                ruka_global.rabotat()
                govorilka.skazat("Возвращаюсь к ассистенту.")

        time.sleep(0.2)

    # перед выходом закрываем камеру если была
    if ruka_global is not None:
        ruka_global.zakryt()


if __name__ == "__main__":
    main()