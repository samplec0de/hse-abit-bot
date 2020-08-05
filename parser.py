import logging
import re
import time

import requests
import urllib3
import xlrd
import xmltodict
from bs4 import BeautifulSoup

FIRST_ABIT_IND = 8
logger = logging.getLogger("BOT")
logging.getLogger("requests").setLevel(logging.WARNING)
programs, campus_id = {}, {}
links = {'Москва': 'https://ba.hse.ru/kolmest2020',
         'Нижний Новгород': 'https://nnov.hse.ru/bacnn/kolmest2020',
         'Пермь': 'https://perm.hse.ru/bacalavr/br',
         'Санкт-Петербург': 'https://spb.hse.ru/ba/kolbak2020'
         }
admission = dict.fromkeys(links.keys())


def update_data():
    global programs, campus_id
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
    for campus in res[1]['row']:
        campus_id[campus['Description'].replace('НИУ ВШЭ - ', '')] = campus['ID']['#text']
    programs = dict.fromkeys(campus_id.values(), None)
    for program in res[2]['row']:
        if not programs[program['RegDepartment']['#text']]:
            programs[program['RegDepartment']['#text']] = {}
        programs[program['RegDepartment']['#text']][program['LearnProgram-D']] = program['ID']['#text']


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


def admission_data():
    for campus, link in zip(links.keys(), links.values()):
        r = requests.get(link)
        bs = BeautifulSoup(r.text, 'html.parser')
        table = bs.find('table')
        rows = table.find_all('tr')[1:]
        if campus in ['Пермь']:
            rows = rows[1:]
        for row in rows:
            data = row.find_all('td')
            if campus in ['Пермь']:
                data = data[1:]
            if len(data) == 1:
                continue
            program_name = re.sub(r'\s\s+', ' ', data[0].text.strip()).replace('»', '"').replace('«', '"')
            program_name = program_name.replace('Центра педагогического мастерства', 'ЦПМ')
            if program_name == 'Итого':
                continue
            elif program_name == 'Юриспру­денция':
                program_name = 'Юриспруденция'
            elif program_name == 'Программ­ная инжене­рия':
                program_name = 'Программная инженерия'

            if not admission[campus]:
                admission[campus] = {}
            admission[campus][program_name] = {'бюджет': data[1].text,
                                               'особое право': data[2].text,
                                               'целевое': data[3].text,
                                               'платное': data[4].text,
                                               'платное для иностранных': data[5].text
                                               }
            for key in admission[campus][program_name]:
                if admission[campus][program_name][key].isdigit():
                    admission[campus][program_name][key] = int(admission[campus][program_name][key])
                else:
                    admission[campus][program_name][key] = 0
    if campus == 'Санкт-Петербург':
        admission[campus]['Политология'] = admission[campus]['Политология и мировая политика']
        admission[campus].pop('Политология и мировая политика', None)
        admission[campus]['Социология'] = admission[campus]['Социология и социальная информатика']
        admission[campus].pop('Социология и социальная информатика', None)
        for i in set(admission[campus].keys()).difference(set(programs[campus_id[campus]])):
            admission[campus][i + ', Санкт-Петербург'] = admission[campus][i]
            admission[campus].pop(i, None)
