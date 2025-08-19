# missing_vowel_app.py
from __future__ import annotations
import json
import random
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import requests  # для вызова Apps Script веб-хуков

# ---------- Константы и пути ----------
BASE_DIR = Path(__file__).resolve().parent
WORDS_FILE = BASE_DIR / "words.csv"          # masked,answer
RESULTS_FILE = BASE_DIR / "results.csv"      # итог «проходов» (локальный лог)
PROGRESS_FILE = BASE_DIR / "progress.json"   # локальный прогресс (fallback)

# если добавите URL в Secrets — будем писать в Google Sheets через Apps Script
# ВРЕМЕННО: жёстко указываем Google Apps Script URL
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwKyhZd_XZgMTl6Ykj4UtwRe8BcS4CUpcHr-Q0aDvhX86K4mPeqripK9pKOUuel66FfBw/exec"
USE_CLOUD = True




VOWELS_RU = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")

# очень простая «интервальная» схема
FIRST_ERROR_INTERVAL = timedelta(minutes=2)
NEXT_ERROR_GROWTH    = timedelta(minutes=8)     # +8 мин за каждый доп. промах
FIRST_SUCCESS_INTERVAL = timedelta(minutes=10)

# ---------- Утилиты ----------
@st.cache_data
def load_words(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8")
    if not {"masked","answer"}.issubset(df.columns):
        raise ValueError("В words.csv должны быть колонки masked,answer")
    return df.dropna(subset=["masked","answer"]).reset_index(drop=True)

def mask_first_two_vowels(word: str) -> str:
    out, c = [], 0
    for ch in word:
        if ch in VOWELS_RU and c < 2:
            out.append("_"); c += 1
        else:
            out.append(ch)
    return "".join(out)

def masked_for_answer(df: pd.DataFrame, answer: str) -> str:
    m = df.loc[df["answer"].str.lower() == answer.lower()]
    return str(m.iloc[0]["masked"]) if not m.empty else mask_first_two_vowels(answer)

def ensure_word(progress: dict, word: str):
    if word not in progress:
        progress[word] = {"errors": 0, "success": 0, "last_seen": None, "next_due": None}

def set_due(progress: dict, word: str, when: datetime | None):
    ensure_word(progress, word)
    progress[word]["next_due"] = when.isoformat(timespec="seconds") if when else None

def get_due(progress: dict, word: str):
    """Возвращает datetime или None, даже если в прогрессе лежит строка/пусто."""
    v = progress.get(word, {}).get("next_due")
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        try:
            # стандартный ISO 'YYYY-MM-DDTHH:MM:SS'
            return datetime.fromisoformat(v.replace("Z", ""))  # на всякий случай убираем Z
        except Exception:
            return None
    return None

def pick_review(progress: dict) -> list[str]:
    now = datetime.now()
    cand = []
    for w, stt in progress.items():
        due = get_due(progress, w)
        if stt.get("errors", 0) > stt.get("success", 0) and (due is None or due <= now):
            cand.append(w)
    random.shuffle(cand)
    return cand

def append_result_row(name: str, score: int, total: int):
    ts = datetime.now().isoformat(timespec="seconds")
    row = {"timestamp": ts, "name": name, "score": score, "total": total}
    if RESULTS_FILE.exists():
        pd.DataFrame([row]).to_csv(RESULTS_FILE, index=False, mode="a", header=False, encoding="utf-8")
    else:
        pd.DataFrame([row]).to_csv(RESULTS_FILE, index=False, encoding="utf-8")

# ---------- Локальный режим (fallback) ----------
def local_load_progress(class_code: str, username: str) -> dict:
    if PROGRESS_FILE.exists():
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return data.get(class_code, {}).get(username, {})
    return {}

def local_save_progress(class_code: str, username: str, progress: dict) -> None:
    data = {}
    if PROGRESS_FILE.exists():
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    data.setdefault(class_code, {})[username] = progress
    PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def local_append_event(row: dict):
    # пишем попытки в RESULTS_FILE (как событийный лог)
    ts = row.get("timestamp", datetime.now().isoformat(timespec="seconds"))
    out = {**row, "timestamp": ts}
    if RESULTS_FILE.exists():
        pd.DataFrame([out]).to_csv(RESULTS_FILE, index=False, mode="a", header=False, encoding="utf-8")
    else:
        pd.DataFrame([out]).to_csv(RESULTS_FILE, index=False, encoding="utf-8")

# ---------- Облако (Apps Script веб-хуки) ----------
def cloud_append_event(row: dict):
    try:
        r = requests.post(APPS_SCRIPT_URL, json=row, timeout=10)
        r.raise_for_status()
    except Exception as e:
        st.warning(f"Не удалось отправить запись в Sheets: {e}")

def cloud_load_progress(class_code: str, username: str) -> dict:
    try:
        r = requests.get(APPS_SCRIPT_URL, params={"class_code": class_code, "username": username}, timeout=10)
        r.raise_for_status()
        data = r.json()
        rows = data.get("rows", [])
    except Exception as e:
        st.warning(f"Не удалось прочитать прогресс из Sheets: {e}")
        rows = []

    prog: dict[str, dict] = {}
    for row in rows:
        w = str(row.get("word", "")).strip()
        if not w:
            continue
        if w not in prog:
            prog[w] = {"errors": 0, "success": 0, "last_seen": None, "next_due": None}

        # success может быть True/False или строка "true"/"false"
        success_flag = str(row.get("success")).lower() in ("true", "1", "yes")
        if success_flag:
            prog[w]["success"] += 1
        else:
            prog[w]["errors"] += 1

        # даты — как строки; пусть лежат строками, а get_due всё распарсит
        prog[w]["last_seen"] = row.get("last_seen") or row.get("timestamp") or None
        prog[w]["next_due"]  = row.get("next_due") or None

    return prog


# ---------- Единый слой сохранения/загрузки ----------
def load_progress(class_code: str, username: str) -> dict:
    if USE_CLOUD:
        return cloud_load_progress(class_code, username)
    return local_load_progress(class_code, username)

def save_event_and_progress(class_code: str, username: str, word: str, progress: dict, ok: bool):
    ensure_word(progress, word)
    now = datetime.now()
    if ok:
        progress[word]["success"] += 1
        due = now + FIRST_SUCCESS_INTERVAL
    else:
        progress[word]["errors"] += 1
        due = now + FIRST_ERROR_INTERVAL + NEXT_ERROR_GROWTH * max(0, progress[word]["errors"] - 1)
    progress[word]["last_seen"] = now.isoformat(timespec="seconds")
    set_due(progress, word, due)

    row = {
        "timestamp": now.isoformat(timespec="seconds"),
        "class_code": class_code,
        "username": username,
        "word": word,
        "success": bool(ok),
        "errors": progress[word]["errors"],
        "success_count": progress[word]["success"],
        "last_seen": progress[word]["last_seen"],
        "next_due": progress[word]["next_due"],
    }

    if USE_CLOUD:
        cloud_append_event(row)
    else:
        local_save_progress(class_code, username, progress)
        local_append_event(row)
        
def pick_review(progress: dict) -> list[str]:
    now = datetime.now()
    cand = []
    for w, stt in progress.items():
        due = get_due(progress, w)  # теперь вернёт datetime или None
        errs = int(stt.get("errors", 0) or 0)
        succ = int(stt.get("success", 0) or 0)
        if errs > succ and (due is None or due <= now):
            cand.append(w)
    random.shuffle(cand)
    return cand

# ---------- UI ----------
st.set_page_config(page_title="Пропущенная гласная — онлайн", page_icon="📝")

st.sidebar.header("👤 Ученик")
class_code = st.sidebar.text_input("Код класса (например, 9Б-2025)", value="demo").strip() or "demo"
username   = st.sidebar.text_input("Ваше имя", value="Гость").strip() or "Гость"
st.sidebar.caption("Хранение прогресса: " + ("Google Sheets ✅" if USE_CLOUD else "локально (JSON/CSV)"))

df = load_words(WORDS_FILE)
total = len(df)

# session state
if "idx" not in st.session_state: st.session_state.idx = 0
if "score" not in st.session_state: st.session_state.score = 0
if "revealed" not in st.session_state: st.session_state.revealed = set()
if "owner" not in st.session_state or st.session_state.owner != (class_code, username):
    st.session_state.progress = load_progress(class_code, username)
    st.session_state.owner = (class_code, username)

st.title("📝 Тренажёр словарных слов (ЕГЭ-2025)")
st.write("Заполняй пропущенные гласные. Ошибки будут повторяться, пока не закрепишь 🔁")

# выбор слова: сперва «долги»
review = pick_review(st.session_state.progress)
use_review = len(review) > 0
if use_review:
    answer = review[0]
    masked = masked_for_answer(df, answer)
else:
    row = df.iloc[st.session_state.idx % total]
    answer = str(row["answer"])
    masked = str(row["masked"])

st.subheader(f"Слово { (st.session_state.idx % total) + 1 } из {total}")
c1, c2 = st.columns([2,1])
with c1:
    st.write("Маска:", f"**{masked}**")
    hint = "".join(answer[i] if (i in st.session_state.revealed or masked[i] != "_") else "_" for i in range(len(answer)))
    st.write("Подсказка:", f"**{hint}**")
with c2:
    if st.button("Подсказка"):
        cand = [i for i,ch in enumerate(answer) if ch in VOWELS_RU and masked[i]=='_' and i not in st.session_state.revealed]
        if cand:
            st.session_state.revealed.add(random.choice(cand))

guess = st.text_input("Ваш вариант:")
if st.button("Проверить"):
    ok = guess.strip().lower() == answer.lower()
    if ok:
        st.success("Верно! ✅")
        st.session_state.score += 1
    else:
        st.error(f"Неверно. Правильный ответ: **{answer}**")

    save_event_and_progress(class_code, username, answer, st.session_state.progress, ok)

    st.session_state.idx += 1
    st.session_state.revealed = set()
    if st.session_state.idx >= total:
        append_result_row(username, st.session_state.score, total)
    st.rerun()

st.info(f"Счёт: {st.session_state.score}")

# Зона учителя
st.sidebar.header("📊 Прогресс (текущий ученик)")
prog = st.session_state.progress
if prog:
    dfp = (pd.DataFrame.from_dict(prog, orient="index")
           .reset_index().rename(columns={"index":"word"})
           .sort_values(["errors","last_seen"], ascending=[False, False]))
    st.sidebar.dataframe(dfp[["word","errors","success","last_seen","next_due"]], height=360)
    st.sidebar.download_button("Скачать прогресс (CSV)",
                               dfp.to_csv(index=False).encode("utf-8"),
                               file_name=f"progress_{class_code}_{username}.csv",
                               mime="text/csv")
else:
    st.sidebar.info("Нет данных по этому имени.")
