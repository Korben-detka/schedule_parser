# -----------------------------------------------------------------------------
# Project Name   : MIET schedule parser
# Organization   : National Research University of Electronic Technology (MIET)
# Department     : Institute of Microdevices and Control Systems
# Author(s)      : Andrei Solodovnikov
# Email(s)       : hepoh@org.miet.ru

# Modified by    : Vyacheslav Rudakov
# Organization   : National Research University of Electronic Technology (MIET)
# Department     : Institute of Microdevices and Control Systems
# Email          : 97rasdvatree@gmail.com
# Fork repository: https://github.com/Korben-detka/schedule_parser

# See https://github.com/MPSU/schedule_parser/blob/master/LICENSE file for
# licensing details.
# ------------------------------------------------------------------------------
import requests
import argparse
import yaml
import sys
import re
from functools import total_ordering
from icalendar import Calendar, Event, Alarm
from datetime import datetime, timedelta
from uuid import uuid4

###############################################################################
# Конфиг по умолчанию
###############################################################################
DEFAULT_CONFIG = {
    "mode": "educator",  # educator | student (задаётся в командной строке)
    "educator": "Солодовников Андрей Павлович",  # если mode = "educator"
    "groups": ["ИВТ-24М", "ИВТ-34"],  # если mode = "educator"
    "group": "ИВТ-14М",  # если mode = "student"
    "academic_hour_duration": 40,  # Длительность академического часа
    "short_recreation_duration": 10,  # Длительность короткой перемены
    "long_recreation_duration": 40,  # Длительность большой перемены
    "semester_starts_at": "05-02-2026",  # Дата начала семестра (первого учебного дня)
    "class_names_cast": {
        "Микропроцессорные средства и системы": "МПСиС",
        "Микропроцессорные системы и средства": "МПСиС",
        "Функциональная верификация": "FV",
        "[ДВ] Универсальная методология верификации (UVM)": "UVM",
    },
    "repeat_number": 5,  # число 4-недельных повторений
    # (4 для 16-ти недель, 5 для добавления 17-18-ых недель)
    "calendar_file_name": "schedule.ics",
    "url": "https://miet.ru/schedule/data",
    "cookie": None,
    "alarm_is_on": True,  # включить/выключить уведомление о парах
    "alarm_minutes_before": 15,  # за сколько минут будет уведомление о парах
    "excluded_disciplines": {"Практическая подготовка"},  # Удаление ненужных дисциплин
    "teacher_in_description": True,  # показывать или нет преподавателя в описании пары
    "class_prefix": "", # space not included, for example "MIET" - > "MIETМПСиС", "MIETUVM" ...
    "class_suffix": ""
}
###############################################################################

###############################################################################
# Обработка аргументов командной строки
###############################################################################
def parse_args():
    parser = argparse.ArgumentParser(description="Парсер расписания МИЭТ в ics-файл")
    parser.add_argument(
        "--mode",
        choices=["educator", "student"],
        required=False,
        help="Режим работы: educator (преподаватель) или student (студент)",
    )
    parser.add_argument(
        "--config",
        type=str,
        required=False,
        help="Путь к yaml-файлу конфигурации (необязательно)",
    )
    parser.add_argument(
        "--group", type=str, required=False, help="Название группы (для режима student)"
    )
    return parser.parse_args()

###############################################################################

###############################################################################
# Применение изменений переданного конфига
###############################################################################
def merge_dicts(default: dict, override: dict) -> dict:
    result = default.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = merge_dicts(result[k], v)
        else:
            result[k] = v
    return result

###############################################################################

url = "https://miet.ru/schedule/data"
cookie = None

###############################################################################
# Класс записи занятия в расписании
# Использует поля, позволяющие однозначно идентифицировать запись, а также
# методы для сравнения записей, вывода их в текстовом виде в консоль и
# проверки на то, что одно занятие является продолжением другого (для
# объединения двойных и более пар в одно занятие).
###############################################################################
@total_ordering
class ScheduleEntry:
    def __init__(
        self, class_name, week_code, room_number, week_day, slot_number, teacher=""
    ):
        self.class_name = class_name  # Название пары
        self.week_code = week_code  # Код недели:  0 — "1-ый числитель",
        #              3 — "2-ой знаменатель"
        self.room_number = room_number  # Номер аудитории
        self.week_day = week_day  # День недели (отсчет ведется с нуля)
        self.slot_number = slot_number  # Номер пары  (отсчет ведется с нуля)
        self.duration = 1  # Длительность занятия в парах
        self.teacher = teacher  # Преподаватель, ведущий пару

    def __eq__(self, other):
        if isinstance(other, ScheduleEntry):
            return (
                self.week_code,
                self.week_day,
                self.slot_number,
                self.room_number,
                self.class_name,
            ) == (
                other.week_code,
                other.week_day,
                other.slot_number,
                other.room_number,
                other.class_name,
            )
        return NotImplemented

    def __lt__(self, other):
        if isinstance(other, ScheduleEntry):
            return (
                self.week_code,
                self.week_day,
                self.slot_number,
                self.room_number,
                self.class_name,
            ) < (
                other.week_code,
                other.week_day,
                other.slot_number,
                other.room_number,
                other.class_name,
            )
        return NotImplemented

    def is_aligned_class(self, other):
        return (
            self.class_name == other.class_name
            and self.week_code == other.week_code
            and self.week_day == other.week_day
            and self.room_number == other.room_number
            and self.teacher == other.teacher
            and abs(self.slot_number - other.slot_number) == 1
        )

    def __repr__(self):
        return f"\n{self.class_name}\n\tweek_code  : {self.week_code}\n\tweek_day   : {self.week_day}\n\troom_number: {self.room_number}\n\tduration   : {self.duration}"

###############################################################################

###############################################################################
# Функция, формирующая название занятия для записи в календаре.
# Позволяет изменить название на аббревиатуру из словаря.
###############################################################################
def get_class_name(name, class_names_cast):
    long_name = name
    class_type = ""
    res_name = ""
    if " [" in name:
        class_type += " [" + name.split(" [")[1]
        long_name = long_name.replace(class_type, "")
    # Проверяем каждый ключ в словаре аббревиатур на вхождение в название предмета
    # (в этом случае не требуется чистить различный мусор, который может быть в
    # названии у дисциплин по выбору)
    for key in sorted(class_names_cast, key=len, reverse=True):
        if key in long_name:
            res_name += class_names_cast[key]
            break
    if not res_name:
        res_name += long_name
    res_name += class_type
    return res_name

###############################################################################

###############################################################################
# Функция, формирующая список занятий, для указанных групп указанного
# преподавателя.
# Проходится по всем занятиям всех указанных групп, и если это занятие ведет
# указанный преподаватель, добавляет это занятие в итоговый список
###############################################################################
def create_list_of_classes_for_educator(config):
    class_list = []
    groups = config["groups"]
    educator = config["educator"]
    url = config["url"]
    cookie = config["cookie"]
    class_names_cast = config["class_names_cast"]
    for group in groups:
        args = {"group": group}
        data = _fetch_json(
            url,
            params={"group": group},
            headers={"Accept": "application/json"},
            cookies=(cookie if isinstance(cookie, dict) else None),
        )
        raw_schedule = data.get("Data", [])

        for double_class in raw_schedule:
            if double_class["Class"]["TeacherFull"] == educator:
                class_list.append(
                    ScheduleEntry(
                        get_class_name(double_class["Class"]["Name"], class_names_cast)
                        + " "
                        + group,
                        double_class["DayNumber"],
                        double_class["Room"]["Name"],
                        double_class["Day"] - 1,  # приводим поля
                        double_class["Time"]["Code"] - 1,  # к нумерации с нуля
                        double_class["Class"]["TeacherFull"],
                    )
                )
    if not class_list:
        print("У преподавателя {} нет занятий в группах {}".format(educator, groups))
        sys.exit(2)
    return class_list

###############################################################################

###############################################################################
# Функция, формирующая список всех занятий указанной группы
###############################################################################
def create_list_of_classes_for_student(config):
    class_list = []

    group = (config.get("group") or "").strip()
    if not group:
        raise ValueError("config['group'] must be a non-empty string")

    url = config["url"]
    cookie_cfg = config.get("cookie") or None

    # Определяем, что именно передавать: заголовок Cookie или cookies-параметр
    headers = None
    cookies = None
    if isinstance(cookie_cfg, dict):
        cookies = cookie_cfg
    elif isinstance(cookie_cfg, str) and cookie_cfg:
        headers = {"Cookie": cookie_cfg}
    if headers is None:
        headers = {"Accept": "application/json"}
    else:
        headers.setdefault("Accept", "application/json")

    data = _fetch_json(url, params={"group": group}, headers=headers, cookies=cookies)
    raw_schedule = data.get("Data", [])

    class_names_cast = config["class_names_cast"]
    for double_class in raw_schedule:
        try:
            class_list.append(
                ScheduleEntry(
                    get_class_name(double_class["Class"]["Name"], class_names_cast),
                    double_class["DayNumber"],
                    double_class["Room"]["Name"],
                    double_class["Day"] - 1,
                    double_class["Time"]["Code"] - 1,
                    double_class["Class"]["TeacherFull"],
                )
            )
        except KeyError:
            # пропускаем некорректные элементы
            continue

    return class_list

###############################################################################

###############################################################################
# Функция, объединяющая двойные и более пары в одну запись.
# Объединяются соседние пары с одинаковым названием, проходящие в один день и
# один тип недели.
# Занятия вида "МПСиС [Лаб] ИВТ-31В" и "МПСиС [Лек] ИВТ-31В" объединены не будут
# даже если они соседние, поскольку названия у них различаются. Тоже самое
# произойдет, если названия полностью одинаковые, но пары не соседние (если
# между ними окно или другая пара).
###############################################################################
def merge_list_of_classes(class_list):
    class_list.sort()
    i = 0
    list_len = len(class_list)
    while i < (list_len - 1):
        if class_list[i].is_aligned_class(class_list[i + 1]):
            class_list[i].duration += 1
            del class_list[i + 1]
            list_len -= 1
            i -= 1
        i += 1
    return class_list

###############################################################################

###############################################################################
# Функци, которые позволяют предположить дату анализируемого семестра.
# В получаемом расписании говорится о том, для какого оно семестра строкой вида:
# "Такой-то семестр XXXX/XXXX" (к примеру: "Осенний семестр 2025/2026")
# Если семестр осенний, то дата его начала по умолчанию — это первый рабочий
# день начиная с первого сентября.
# Если семестр весенний, то дата его начала по умолчанию — это второй
# понедельник февраля.
###############################################################################
def calculate_semester_start(config):
    """Динамически вычисляет дату начала семестра."""
    url = config["url"]
    group_for_semester = config.get("group") or (config.get("groups") or [None])[0]
    data = _fetch_json(
        url,
        params={"group": group_for_semester},
        headers={"Accept": "application/json"},
        cookies=(
            config.get("cookie") if isinstance(config.get("cookie"), dict) else None
        ),
    )
    semester = data["Semestr"]

    d = None
    year_pos = semester.find("/")
    if semester.startswith("Осенний"):
        year_pos = year_pos - 4
        d = datetime(int(semester[year_pos : year_pos + 4]), 9, 1)
        if d.weekday() >= 5:
            d += timedelta(days=(7 - d.weekday()))
    else:
        year_pos = year_pos + 1
        d = datetime(int(semester[year_pos : year_pos + 4]), 2, 1)
        while d.weekday() != 0:
            d += timedelta(days=1)
        d += timedelta(days=7)
    semester_starts_at = d.strftime("%d-%m-%Y")
    return semester_starts_at

###############################################################################
# Функция, создающая ics-файл по сформированному списку занятий
###############################################################################
def create_ics_file(schedule, config):
    start_date = config["semester_starts_at"]
    academic_hour_duration = config["academic_hour_duration"]
    short_recreation_duration = config["short_recreation_duration"]
    long_recreation_duration = config["long_recreation_duration"]
    file_name = config["calendar_file_name"]
    repeat_number = config["repeat_number"]
    teacher_on = config["teacher_in_description"]

    # Преобразуем строку в дату
    start_date = datetime.strptime(start_date, "%d-%m-%Y")

    # Определяем день недели первого учебного дня (0 - понедельник, 6 - воскресенье)
    first_day_of_semester = start_date.weekday()

    # Создаем объект календаря
    cal = Calendar()
    cal.add("prodid", "-//MIET//Schedule Parser//RU")
    cal.add("version", "2.0")

    # Определяем продолжительность пары
    pair_duration = academic_hour_duration * 2

    # Проходимся по всем записям расписания
    for entry in schedule:
        # Определяем продолжительность занятия
        class_duration = (
            entry.duration * pair_duration
            + (entry.duration - 1) * short_recreation_duration
        )

        # Вычисляем смещение для первой недели с учетом дня недели начала семестра
        if (entry.week_day < first_day_of_semester) and (entry.week_code == 0):
            # Если целевой день недели 1-ой учебной недели идет до первого учебного
            # дня, переносим занятие на следующую итерацию "1-го числителя"
            week_offset = (entry.week_code + 4) * 7
            day_offset = entry.week_day - first_day_of_semester
            first_class_date = start_date + timedelta(days=week_offset + day_offset)
        else:
            # Если целевой день недели идет во время или после дня недели первого
            # учебного дня или если это занятие не первой учебной недели
            week_offset = entry.week_code * 7
            day_offset = entry.week_day - first_day_of_semester
            first_class_date = start_date + timedelta(days=week_offset + day_offset)

        # Определяем время начала пары
        # Первая пара начинается в 9:00
        # Учитываем 10-минутные перемены между парами и 40 минут после второй пары
        start_time = first_class_date + timedelta(hours=9)  # Начало первой пары
        start_time += timedelta(
            minutes=entry.slot_number * (pair_duration + 10)
        )  # Смещение для каждой пары

        # Учитываем, что перемена после второй пары составляет 40 минут
        if entry.slot_number >= 2:  # TODO check this
            start_time += timedelta(
                minutes=(long_recreation_duration - short_recreation_duration)
            )

        # Продолжительность пары
        end_time = start_time + timedelta(minutes=class_duration)

        # Создаем событие
        event = Event()
        prefix = config.get("class_prefix", "")
        suffix = config.get("class_suffix", "")
        event.add("summary", f"{prefix}{entry.class_name}{suffix}")
        event.add("dtstart", start_time)
        event.add("dtend", end_time)
        event.add("location", entry.room_number)
        event.add("uid", str(uuid4()))
        if teacher_on:
            event.add("description", entry.teacher)

        # Устанавливаем правило повторения
        event.add("rrule", {"freq": "weekly", "interval": 4, "count": repeat_number})

        # Создаем напоминание (уведомление)
        alarm = Alarm()
        if config["alarm_is_on"]:
            alarm.add("action", "DISPLAY")
        else:
            alarm.add("action", "NONE")

        alarm.add("description", f"Reminder: {entry.class_name} in {entry.room_number}")
        alarm.add(
            "trigger", timedelta(minutes=-config["alarm_minutes_before"])
        )  # За alarm_minutes_before начала пары

        # Добавляем напоминание в событие
        event.add_component(alarm)

        # Добавляем событие в календарь
        cal.add_component(event)

    # Записываем календарь в файл
    with open(file_name, "wb") as f:
        f.write(cal.to_ical())

def base_class_name(name: str) -> str:
    # отрезаем всё после первой скобки […
    name = name.split(" [")[0]
    # если в преподавательском режиме к названию приписана группа,
    # убираем последнюю словесную часть вида "… ИВТ-31В"
    return re.sub(r"\s+[А-ЯA-Z\-0-9]{3,}$", "", name).strip()

def _fetch_json(url, params, headers=None, cookies=None, timeout=(5, 10)):
    resp = requests.get(
        url=url, params=params, headers=headers, cookies=cookies, timeout=timeout
    )
    resp.raise_for_status()  # поднимет HTTPError при 4xx/5xx
    try:
        return resp.json()
    except ValueError:
        ct = resp.headers.get("Content-Type", "")
        preview = resp.text[:200]
        raise RuntimeError(
            f"Non-JSON response: status={resp.status_code}, content-type={ct}, body[:200]={preview!r}"
        )

###############################################################################

def main():
    args = parse_args()

    config = DEFAULT_CONFIG.copy()
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                yaml_config = yaml.safe_load(f) or {}
            config = merge_dicts(DEFAULT_CONFIG, yaml_config)
        except Exception as e:
            print(f"Ошибка чтения {args.config}: {e}")
            sys.exit(1)

    # Переопределяем mode из аргументов, если указан
    if args.mode:
        config["mode"] = args.mode

    if config["semester_starts_at"] is None:
        config["semester_starts_at"] = calculate_semester_start(config)
        print(
            f"Не указана дата начала семестра.\n"
            f"Начало семестра автоматически определено как {config['semester_starts_at']}, "
            f"проверьте что эта дата верна!"
        )

    # Получаем данные в зависимости от режима
    if config["mode"] == "educator":
        unmerged = create_list_of_classes_for_educator(config)
    else:
        # Переопределяем group из аргументов, только если указан
        if args.group:
            config["group"] = args.group
        unmerged = create_list_of_classes_for_student(config)

    # Фильтруем исключенные дисциплины
    excluded = set(config["excluded_disciplines"])

    # Если исключённая дисциплина переименовывается (есть в class_names_cast),
    # то исключаем и её
    for long_name, short_name in config["class_names_cast"].items():
        if long_name in excluded:
            excluded.add(short_name)

    unmerged_class_list = [
        entry
        for entry in unmerged
        if base_class_name(entry.class_name) not in excluded
    ]

    merged = merge_list_of_classes(unmerged_class_list)
    create_ics_file(merged, config)

if __name__ == "__main__":
    main()
