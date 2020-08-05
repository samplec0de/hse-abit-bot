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

import parser

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
    xls_id = parser.programs[parser.campus_id[campus]][program]
    stats = program_stats(xls_id)
    xls_url = f'https://priem8.hse.ru/abitreports/bachreports/{xls_id}.xls'
    try:
        req = requests.get(xls_url)
    except urllib3.connection.VerifiedHTTPSConnection as e:
        raise Exception(e)
    if req.status_code != 200:
        raise Exception(f"Status code {req.status_code}")
    book = xlrd.open_workbook(file_contents=req.content)
    sheet = book.sheet_by_index(0)
    for i in range(sheet.ncols):
        value = sheet.cell_value(6, i)
        if value == 'Сумма конкурсных баллов':
            sum_ind = i
        elif value == 'Форма обучения':
            edu_form = i
        elif value == 'Требуется общежитие на время обучения':
            dominatory = i
    bvi_count, agreement_count, celevoe_count, osoboe_pravo_count, dormitory_count = 0, 0, 0, 0, 0
    agreement_celevoe_count = 0
    commercial_count, govsponsor_count, combined_count = 0, 0, 0
    ege_govsponsor, bvi, osoboe_pravo, celevoe = [], [], [], []
    total_abits = sheet.nrows - parser.FIRST_ABIT_IND + 1
    program_places = parser.admission[campus].get(program)
    program_places = program_places if program_places is not None else {'бюджет': stats['govsponsor'],
                                                                        'особое право': 0,
                                                                        'целевое': 0,
                                                                        'платное': stats['paid'],
                                                                        'платное для иностранных': 0
                                                                        }
    selected_abit = None
    place, sogl_place = '', ''
    for i in range(parser.FIRST_ABIT_IND, sheet.nrows):
        abit_fio = sheet.cell_value(i, 2)
        abit_score = sheet.cell_value(i, sum_ind)
        abit_bvi = sheet.cell_value(i, 3)
        abit_osoboe_pravo = sheet.cell_value(i, 4)
        abit_celevoi = sheet.cell_value(i, 5)
        abit_agreement = sheet.cell_value(i, 6)
        abit_edu_form = sheet.cell_value(i, edu_form)
        abit_dormitory = sheet.cell_value(i, dominatory)
        bvi_count += 1 if abit_bvi else 0
        agreement_count += 1 if abit_agreement == 'Да' else 0
        agreement_celevoe_count += 1 if abit_agreement == 'Да' and abit_celevoi == '+' else 0
        celevoe_count += 1 if abit_celevoi == '+' else 0
        osoboe_pravo_count += 1 if abit_osoboe_pravo == '+' else 0
        dormitory_count += 1 if abit_dormitory == '+' else 0
        commercial_count += 1 if 'К' in abit_edu_form else 0
        govsponsor_count += 1 if 'Б' in abit_edu_form else 0
        combined_count += 1 if edu_form == 'Б,К' else 0
        abit = {'fio': abit_fio, 'score': abit_score,
                'bvi': abit_bvi,
                'osoboe_pravo': abit_osoboe_pravo,
                'celevoi': abit_celevoi,
                'agreement': abit_agreement,
                'edu_form': abit_edu_form,
                'dormitory': abit_dormitory
                }
        if abit_bvi:  # по БВИ
            bvi.append(abit)
        elif abit_osoboe_pravo == '+':  # Поступление на места в рамках квоты  для лиц, имеющих особое право
            osoboe_pravo.append(abit)
        elif abit_celevoi == '+':  # Поступление на места в рамках квоты целевого приема
            celevoe.append(abit)
        elif 'Б' in abit_edu_form:  # По ЕГЭ на бюджет
            ege_govsponsor.append(abit)
        if 'fio' in user and user['fio']:
            if user['fio'] == abit_fio:
                selected_abit = abit
                if abit_bvi:
                    sogl_place = f"    👍 Вы поступаете по БВИ\n"
                elif abit_osoboe_pravo == '+':
                    sogl_place = agreement_count - len(bvi) + (1 if abit_agreement == 'Нет' else 0)
                elif abit_celevoi == '+':
                    sogl_place = agreement_celevoe_count + (1 if abit_agreement == 'Нет' else 0)
                else:
                    sogl_place = agreement_count
                    place = i - parser.FIRST_ABIT_IND + 1
                    place -= max(0, program_places['целевое'] - celevoe_count)
                    place -= max(0, program_places['особое право'] - osoboe_pravo_count)
                    place = f"    Бюджет: {place}\n"
                if type(sogl_place) == int:
                    sogl_place = f"    По согласиям: {sogl_place}\n"
    ege_places = stats["govsponsor"] + stats["hsesponsor"]
    ege_places -= bvi_count
    ege_places -= min(osoboe_pravo_count, program_places['особое право'])
    ege_places -= min(celevoe_count, program_places['целевое'])
    temp_count = 0
    temp_index = 0
    last_abit_score = 0
    while temp_count < ege_places and temp_index < len(ege_govsponsor):
        if ege_govsponsor[temp_index]['agreement'] == 'Да':
            temp_count += 1
            last_abit_score = ege_govsponsor[temp_index]['score']
        temp_index += 1
    if ege_places <= 0:
        govsponsor_score = 'БВИ'
    else:
        govsponsor_score = ege_govsponsor[ege_places - 1]['score']
    if len(bvi) <= stats["govsponsor"]:
        is_kvazi = f'✅ Все бви помещаются в бюджет за счет государства' \
                   f' (свободно {ege_places} мест по ЕГЭ)'
    else:
        is_kvazi = '❌ Бви не помещаются в бюджет за счет государства, ' \
                   'после ранжирования кто-то попадет на бюджет за счёт вышки.'
    for_user, your_place = '', ''
    if 'fio' in user and user['fio']:
        for_user = f"Выбранный абитуриент: {user['fio']}\n"
        if selected_abit:
            your_place = '👤 Ваши места:\n' + place + sogl_place
    message = f'Вы отслеживаете направление <a href="{xls_url}">"{program}" ({campus})</a>\n\n' \
              f'📄 Всего заявлений: {total_abits}\n' \
              f'😳 Бюджет: {govsponsor_count}/{program_places["бюджет"]} (бви {len(bvi)}, ' \
              f'всего {stats["govsponsor"]} + {stats["hsesponsor"]} за счёт ВШЭ)\n' \
              f'💰 Контракт: {commercial_count} (всего {stats["paid"]})\n' \
              f'🤑 Бюджет, контракт: {combined_count}\n' \
              f'🤝 С согласием на зачисление: {agreement_count}\n' \
              f'🏚 С общежитием: {dormitory_count}\n' \
              f'😉 По особому праву: {osoboe_pravo_count}/{program_places["особое право"]}\n' \
              f'🏭 Целевое: {celevoe_count}/{program_places["целевое"]}\n\n' \
              f'{is_kvazi}\n\n' \
              f'<code>📊 Проходные бюджет:\n' \
              f'    Общий: {govsponsor_score}\n' \
              f'    По согласиям: {last_abit_score}\n' \
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
        try:
            message = program_board(selected_campus, selected_program, user)
        except:
            print(traceback.format_exc())
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


def inlinequery(update: Update, context: CallbackContext):
    query = update.inline_query.query
    if query.startswith('Начните писать название: '):
        query = query.replace('Начните писать название: ', '').lower()
        results = []
        result = users.find_one({'user_id': update.inline_query.from_user.id})
        if not result:
            return
        user_campus = result['campus']
        for program in parser.programs[parser.campus_id[user_campus]]:
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
        xls_id = parser.programs[parser.campus_id[user['campus']]][user['program']]
        abits = parser.get_abits(xls_id)
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


if __name__ == '__main__':
    last_refresh = {}
    logger = logging.getLogger("BOT")
    logging.getLogger("requests").setLevel(logging.WARNING)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.setLevel(logging.INFO)
    parser.update_data()
    parser.admission_data()
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
    merged_programs = set(list(list(parser.programs.values())[0].keys())
                          + list(list(parser.programs.values())[1].keys())
                          + list(list(parser.programs.values())[2].keys())
                          + list(list(parser.programs.values())[3].keys()))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(f'({"|".join(merged_programs)})'), set_program))
    updater.dispatcher.add_handler(InlineQueryHandler(inlinequery))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=refresh, pattern='^update$'))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=rating, pattern='^rating'))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=close, pattern='^close'))
    updater.dispatcher.add_handler(CallbackQueryHandler(callback=change_abit, pattern='^change_abit'))
    updater.start_polling()
    updater.idle()
