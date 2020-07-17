import logging
import math
import os
import time
import traceback
from datetime import datetime
from typing import Any, List, Dict
from uuid import uuid4

import pymongo
import requests
import urllib3
import xlrd
import xmltodict
from pymongo.collection import Collection
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, \
    InlineQueryResultArticle, InputTextMessageContent, CallbackQuery, ParseMode
from telegram.ext import Updater, CallbackContext, CommandHandler, MessageHandler, Filters, InlineQueryHandler, \
    CallbackQueryHandler

COOLDOWN = 15
CLOSE_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton('Закрыть', callback_data='close')]])
NOT_IMPLEMENTED = 'Спасибо. Этот функционал будет добавлен в ближайшее время.'
CAMPUSES = ['Москва', 'Санкт-Петербург', 'Нижний Новгород', 'Пермь']
SET_CAMPUS = 0
SET_PROGRAM = 1
SET_FIO = 2
LOOK_PROGRAM = 3


def error(update: Update, context: CallbackContext):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, context.error)


def start(update: Update, context: CallbackContext):
    keyboard = [[CAMPUSES[i], CAMPUSES[i + 1]] for i in range(0, len(CAMPUSES), 2)]
    update.message.reply_text('Выберите кампус', reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    state.delete_one({'user_id': update.message.chat.id})
    users.delete_one({'user_id': update.message.chat.id})
    set_state(update.message.chat.id, SET_CAMPUS)


def get_state(user_id: int):
    user_state = state.find_one({'user_id': user_id})
    if user_state:
        return user_state['state']


def set_state(user_id: int, new_state: int):
    if not state.find_one_and_update({'user_id': user_id}, {'$set': {'state': new_state}}):
        state.insert_one({'user_id': user_id, 'state': new_state})


def get_user(user_id: int):
    return users.find_one({'user_id': user_id})


def set_user_param(user_id: int, key: str, value: Any):
    return users.find_one_and_update({'user_id': user_id}, {'$set': {key: value}})


def set_campus(update: Update, context: CallbackContext):
    selected_campus = update.message.text
    user_state = get_state(update.message.chat.id)
    if user_state == SET_CAMPUS:
        users.insert_one({'user_id': update.message.chat.id,
                          'username': update.message.chat.username,
                          'campus': selected_campus})
        keyboard = [[InlineKeyboardButton('Найти', switch_inline_query_current_chat='Начните писать название: ')]]
        ReplyKeyboardRemove()
        reply_markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text('Спасибо', reply_markup=ReplyKeyboardRemove())
        update.message.reply_text('Выберите направление подготовки', reply_markup=reply_markup)
        set_state(update.message.chat.id, SET_PROGRAM)
    else:
        update.message.reply_text('Сообщение не распознано. Перезапустите бота /start')


def set_abit(update: Update, context: CallbackContext):
    selected_abit = update.message.text.replace('Абитуриент: ', '')
    user_state = get_state(update.message.chat.id)
    if user_state == SET_FIO:
        set_user_param(update.message.chat.id, 'fio', selected_abit)
        update.message.reply_text('Спасибо. Теперь нажмите "Обновить" в прошлом сообщении.', reply_markup=CLOSE_MARKUP)
        context.bot.delete_message(update.message.chat.id, update.message.message_id)
        context.bot.delete_message(update.message.chat.id, get_user(update.message.chat.id)['bot_temp_message_id'])
    else:
        update.message.reply_text('Сообщение не распознано. Перезапустите бота /start')


def program_board(campus: str, program: str, user: dict):
    xls_id = programs[campus_id[campus]][program]
    abits = get_abits(xls_id)
    stats = program_stats(xls_id)
    edu_form_data = edu_form(abits)
    if len(bvi(abits)) <= stats["govsponsor"]:
        is_kvazi = f'✅ Все бви помещаются в бюджет за счет государства' \
                   f' (свободно {stats["govsponsor"] - len(bvi(abits))} мест)'
    else:
        is_kvazi = '❌ Бви не помещаются в бюджет за счет государства, ' \
                   'после ранжирования кто-то попадет на бюджет за счёт вышки.'
    if max(0, len(edu_form_data["Б,К"]) - stats["govsponsor"] - len(bvi(abits))) + len(edu_form_data["К"]) \
            <= stats["paid"]:
        no_paid_competition = '✅ Нет конкурса на платное (кол-во претендующих меньше количества мест)'
    else:
        no_paid_competition = '❓ Возможен конкурс на платное (Б,К + К > кол-во мест)'
    xls_url = f'https://priem8.hse.ru/abitreports/bachreports/{xls_id}.xls'
    non_bvi = set(abits.keys()).difference(bvi(abits))
    govsponsor = [{'fio': abit, **abits[abit]} for abit in non_bvi.difference(edu_form_data["К"])]
    govsponsor_minus_bvi = [abit for abit in govsponsor if not abit['bvi']]
    govsponsor.sort(key=lambda f: -f['score'])
    non_bvi_places = stats["govsponsor"] - len(bvi(abits))
    if len(govsponsor_minus_bvi) <= non_bvi_places and govsponsor:
        govsponsor_score = govsponsor[-1]
    else:
        if govsponsor_minus_bvi:
            govsponsor_score = govsponsor_minus_bvi[non_bvi_places - 1]
        else:
            govsponsor_score = 'на бюджет никто не поступает'
    if non_bvi_places == 0:
        govsponsor_score = 'бви (обратите внимание, бот не умеет работать с квази-бюджетом)'
    elif type(govsponsor_score) == dict:
        govsponsor_score = str(int(govsponsor_score['score']))
    with_soglasie_minus_bvi = [abit for abit in govsponsor if not abit['bvi'] and abit['agreement'] == 'Да']
    if len(with_soglasie_minus_bvi) <= non_bvi_places:
        govsponsor_soglasie_score = int(with_soglasie_minus_bvi[-1]['score'])
    else:
        if with_soglasie_minus_bvi:
            govsponsor_soglasie_score = int(with_soglasie_minus_bvi[non_bvi_places - 1]['score'])
        else:
            govsponsor_soglasie_score = 'на бюджет никто не поступает'
    if non_bvi_places == 0:
        govsponsor_soglasie_score = 'бви (обратите внимание, бот не умеет работать с квази-бюджетом)'
    place = ''
    sogl_place = ''
    for_user = ''
    govsponsor_sogl = [abit for abit in govsponsor if abit['agreement'] == 'Да']

    if 'fio' in user and user['fio'] in [abit['fio'] for abit in govsponsor] + bvi(abits):
        user_abit = {}
        for fio, abit in zip(abits.keys(), abits.values()):
            if fio == user['fio']:
                user_abit = abit
                break
        sogl_place = 1 + len(bvi(abits))
        for_user = f"Выбранный абитуриент: {user['fio']}\n"
        for abit in govsponsor_sogl:
            if abit['score'] >= get_score(abits, user['fio']) and abit['fio'] != user['fio'] and not abit['bvi']:
                sogl_place += 1
        sogl_place = f"    Бюджет с согласием: {sogl_place}\n"
        if user_abit['bvi']:
            sogl_place = f"    👍 Вы поступаете по БВИ\n"
        for i, abit in enumerate(govsponsor):
            if abit['fio'] == user['fio']:
                place = f"    Бюджет: {i + 1}\n"
    your_place = '👤 Ваши места:\n' + place + sogl_place if len(place + sogl_place) > 0 else ''
    message = f'Вы отслеживаете направление <a href="{xls_url}">"{program}" ({campus})</a>\n\n' \
              f'📄 Всего заявлений: {len(abits)}\n' \
              f'😳 Бюджет: {len(edu_form_data["Б"])} (бви {len(bvi(abits))}, ' \
              f'всего {stats["govsponsor"]} + {stats["hsesponsor"]} за счёт ВШЭ)\n' \
              f'💰 Контракт: {len(edu_form_data["К"])} (всего {stats["paid"]})\n' \
              f'🤑 Бюджет, контракт: {len(edu_form_data["Б,К"])}\n' \
              f'🤝 С согласием на зачисление: {len(soglasie(abits))}\n' \
              f'🏚 С общежитием: {len(dormitory(abits))}\n' \
              f'🏭 Целевое: {len(celevoe(abits))}\n\n' \
              f'{is_kvazi}\n' \
              f'{no_paid_competition}\n\n' \
              f'<code>📊 Проходные бюджет:\n' \
              f'    Общий: {govsponsor_score}\n' \
              f'    По согласиям: {govsponsor_soglasie_score}\n' \
              f'{your_place}</code>' \
              f'{for_user}' \
              f'🕔 Последнее обновление {datetime.now().strftime("%d.%m.%Y %H:%M:%S")} (база ВШЭ: {stats["hsetime"]})'
    return message


def set_program(update: Update, context: CallbackContext):
    selected_program = update.message.text
    user_state = get_state(update.message.chat.id)
    if user_state == SET_PROGRAM:
        users.find_one_and_update({'user_id': update.message.chat.id}, {'$set': {'program': selected_program}})
        user = get_user(update.message.chat.id)
        selected_campus = user['campus']
        try:
            message = program_board(selected_campus, selected_program, user)
            keyboard_inline = [[InlineKeyboardButton("🔄 Обновить", callback_data="update")],
                               [InlineKeyboardButton("📊 Определить своё место в рейтинге", callback_data="rating")]]
            update.message.reply_text(message,
                                      reply_markup=InlineKeyboardMarkup(keyboard_inline),
                                      parse_mode=ParseMode.HTML)
            set_state(update.message.chat.id, LOOK_PROGRAM)
        except Exception as e:
            print(traceback.format_exc())
            update.message.reply_text('Ой! Мне не удалось получить информацию с сайта ВШЭ. Попробуй позднее.')


def refresh(update: Update, context: CallbackContext):
    query: CallbackQuery = update.callback_query
    user_id = query.message.chat.id
    if user_id not in last_refresh or time.time() - last_refresh[user_id] > COOLDOWN:
        last_refresh[user_id] = time.time()
        user = get_user(user_id)
        selected_campus = user['campus']
        selected_program = user['program']
        message = program_board(selected_campus, selected_program, user)
        keyboard_inline = [[InlineKeyboardButton("🔄 Обновить", callback_data="update")],
                           [InlineKeyboardButton("📊 Определить место в рейтинге", callback_data="rating")]]
        if 'fio' in user:
            keyboard_inline = keyboard_inline[:-1]
            keyboard_inline[0].append(InlineKeyboardButton("Изменить абитуриента", callback_data="change_abit"))
        query.edit_message_text(text=message, parse_mode=ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup(keyboard_inline))
        query.answer(text='Информация обновлена')
    else:
        query.answer(text=f'Не так часто! '
                          f'Подождите ещё {COOLDOWN - math.ceil(time.time() - last_refresh[user_id])} секунд.')


def rating(update: Update, context: CallbackContext):
    query: CallbackQuery = update.callback_query
    user_id = query.message.chat.id
    user = get_user(user_id)
    if 'fio' in user:
        refresh(update, context)
    else:
        keyboard = [[InlineKeyboardButton('Найти', switch_inline_query_current_chat='Начните писать своё имя: ')]]
        msg_id = context.bot.send_message(user_id,
                                          'Выберите абитуриента',
                                          reply_markup=InlineKeyboardMarkup(keyboard)).message_id
        set_user_param(user_id, 'bot_temp_message_id', msg_id)
        set_state(user_id, SET_FIO)


def change_abit(update: Update, context: CallbackContext):
    query: CallbackQuery = update.callback_query
    user_id = query.message.chat.id
    keyboard = [[InlineKeyboardButton('Найти', switch_inline_query_current_chat='Начните писать своё имя: ')]]
    msg_id = context.bot.send_message(user_id,
                                      'Выберите абитуриента',
                                      reply_markup=InlineKeyboardMarkup(keyboard)).message_id
    set_user_param(user_id, 'bot_temp_message_id', msg_id)
    set_state(user_id, SET_FIO)


def get_score(abits: List[Dict[str, Any]], fio: str):
    for abit in abits:
        if abit == fio:
            return abits[abit]['score']
    return -1


def bvi(data: dict):
    result = []
    for abit, data in zip(data.keys(), data.values()):
        if data['bvi']:
            result.append(abit)
    return result


def soglasie(data: dict):
    result = []
    for abit, data in zip(data.keys(), data.values()):
        if data['agreement'] == 'Да':
            result.append({'fio': abit, **data})
    return result


def dormitory(data: dict):
    result = []
    for abit, data in zip(data.keys(), data.values()):
        if data['dormitory'] == '+':
            result.append(abit)
    return result


def celevoe(data: dict):
    result = []
    for abit, data in zip(data.keys(), data.values()):
        if data['celevoi'] == '+':
            result.append(abit)
    return result


def edu_form(data: dict):
    result = {'Б': [], 'К': [], 'Б,К': []}
    for abit, data in zip(data.keys(), data.values()):
        result[data['edu_form']].append(abit)
    return result


def program_stats(xls_id: int):
    try:
        req = requests.get(f'https://priem8.hse.ru/abitreports/bachreports/{xls_id}.xls')
    except urllib3.connection.VerifiedHTTPSConnection as e:
        raise Exception(e)
    if req.status_code != 200:
        raise Exception(f"Status code {req.status_code}")
    book = xlrd.open_workbook(file_contents=req.content)
    sheet = book.sheet_by_index(0)
    info = sheet.cell_value(3, 1).split('\n')
    result = {'year': info[0].strip().split(': ')[-1],
              'govsponsor': info[1].strip().split(': ')[-1],
              'hsesponsor': info[2].strip().split(': ')[-1],
              'paid': info[3].split(': ')[-1],
              'hsetime': sheet.cell_value(4, 1).strip().split(': ')[-1]}
    for key in result:
        if result[key] and result[key].isdigit():
            result[key] = int(result[key])
        elif key not in ['hsetime']:
            result[key] = 0
    return result


def get_abits(xls_id: int):
    try:
        req = requests.get(f'https://priem8.hse.ru/abitreports/bachreports/{xls_id}.xls')
    except urllib3.connection.VerifiedHTTPSConnection as e:
        raise Exception(e)
    if req.status_code != 200:
        raise Exception(f"Status code {req.status_code}")
    book = xlrd.open_workbook(file_contents=req.content)
    sheet = book.sheet_by_index(0)
    sum_ind = -1
    edu_form = -1
    dominatory = -1
    for i in range(sheet.ncols):
        value = sheet.cell_value(6, i)
        if value == 'Сумма конкурсных баллов':
            sum_ind = i
        elif value == 'Форма обучения':
            edu_form = i
        elif value == 'Требуется общежитие на время обучения':
            dominatory = i
    FIRST_ABIT_IND = 8
    abits = {}
    for i in range(FIRST_ABIT_IND, sheet.nrows):
        abit_fio = sheet.cell_value(i, 2)
        abit_score = sheet.cell_value(i, sum_ind)
        abit_bvi = sheet.cell_value(i, 3)
        abit_osoboe_pravo = sheet.cell_value(i, 4)
        abit_celevoi = sheet.cell_value(i, 5)
        abit_agreement = sheet.cell_value(i, 6)
        abit_edu_form = sheet.cell_value(i, edu_form)
        abit_dormitory = sheet.cell_value(i, dominatory)
        abits[abit_fio] = {'score': abit_score,
                           'bvi': abit_bvi,
                           'osoboe_pravo': abit_osoboe_pravo,
                           'celevoi': abit_celevoi,
                           'agreement': abit_agreement,
                           'edu_form': abit_edu_form,
                           'dormitory': abit_dormitory}
    return abits


def inlinequery(update: Update, context: CallbackContext):
    query = update.inline_query.query
    if query.startswith('Начните писать название: '):
        query = query.replace('Начните писать название: ', '').lower()
        results = []
        result = users.find_one({'user_id': update.inline_query.from_user.id})
        if not result:
            return
        user_campus = result['campus']
        for program in programs[campus_id[user_campus]]:
            if query in program.lower() or query == 'все':
                results.append(InlineQueryResultArticle(
                    id=uuid4(),
                    title=program,
                    input_message_content=InputTextMessageContent(program)))
        update.inline_query.answer(results[:50])
        set_state(update.inline_query.from_user.id, SET_PROGRAM)
    elif query.startswith('Начните писать своё имя: '):
        query = query.replace('Начните писать своё имя: ', '').lower()
        user = get_user(update.inline_query.from_user.id)
        xls_id = programs[campus_id[user['campus']]][user['program']]
        abits = get_abits(xls_id)
        results = []
        for abit in abits:
            if query in abit.lower() or query == 'все':
                results.append(InlineQueryResultArticle(
                    id=uuid4(),
                    title=abit,
                    input_message_content=InputTextMessageContent('Абитуриент: ' + abit)))
        update.inline_query.answer(results[:50])


def close(update: Update, context: CallbackContext):
    query = update.callback_query
    context.bot.delete_message(chat_id=query.message.chat.id, message_id=query.message.message_id)
    query.answer()


def update_data():
    global programs, campus_id
    parse_request = None
    while True:
        try:
            parse_request = requests.post('https://priem8.hse.ru/hseAnonymous/batch.xml', data={
                'query': '<root><query class="TTimePoint" fetchall="1"><item part="0" name="Passed"/>'
                         '<item part="0" name="Name"/><item part="1" name="Master$N" value="BachAbitAdmission"/>'
                         '</query><query class="TRegDepartment" fetchall="1"><item part="0" name="ID"/>'
                         '<item part="0" name="Description"/><item part="1" name="AdmModeratorPosition" value="*"/>'
                         '<item part="2" name="IsCentral" special="7"/><item part="2" name="AdmModeratorPosition"/>'
                         '</query><query class="TBachCompetition" fetchall="1">'
                         '<item part="0" name="ID"/><item part="0" name="OfficialName"/>'
                         '<item part="0" name="LearnProgram$D"/>'
                         '<item part="0" name="RegDepartment"/>'
                         '<item part="0" name="ForeignExams"/><item part="1" name="Master" value="3656465518"/>'
                         '<item part="1" name="NotPublishLists" value="0"/><item part="2" name="OfficialName"/>'
                         '<item part="2" name="LearnProgram$D"/></query></root>'})
            break
        except requests.exceptions.ConnectionError:
            logger.info("Пытаюсь подключиться к сайту ВШЭ...")
            time.sleep(0.5)
    res = xmltodict.parse(parse_request.text)['batch']['data']
    campus_id = {}
    for campus in res[1]['row']:
        campus_id[campus['Description'].replace('НИУ ВШЭ - ', '')] = campus['ID']['#text']
    programs = dict.fromkeys(campus_id.values(), None)
    for program in res[2]['row']:
        if not programs[program['RegDepartment']['#text']]:
            programs[program['RegDepartment']['#text']] = {}
        programs[program['RegDepartment']['#text']][program['LearnProgram-D']] = program['ID']['#text']


if __name__ == '__main__':
    programs = {}
    campus_id = {}
    last_refresh = {}
    logger = logging.getLogger("BOT")
    logging.getLogger("requests").setLevel(logging.WARNING)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.setLevel(logging.INFO)
    update_data()
    mongo = pymongo.MongoClient(os.environ.get('mongo_uri'))
    db = mongo['hse-abit']
    users: Collection = db['users']
    state: Collection = db['state']
    updater: Updater = Updater(os.environ["token"], use_context=True)
    updater.dispatcher.add_error_handler(error)
    updater.dispatcher.add_handler(CommandHandler('start', start))
    updater.dispatcher.add_handler(CommandHandler('restart', start))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(f'({"|".join(CAMPUSES)})'), set_campus))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(f'^Абитуриент: .*$'), set_abit))
    merged_programs = set(list(list(programs.values())[0].keys())
                          + list(list(programs.values())[1].keys())
                          + list(list(programs.values())[2].keys())
                          + list(list(programs.values())[3].keys()))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(f'({"|".join(merged_programs)})'), set_program))
    updater.dispatcher.add_handler(InlineQueryHandler(inlinequery))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=refresh, pattern='^update$'))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=rating, pattern='^rating'))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=close, pattern='^close'))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=change_abit, pattern='^change_abit'))
    updater.start_polling()
    updater.idle()
