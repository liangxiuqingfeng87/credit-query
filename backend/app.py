# -*- coding: utf-8 -*-
"""
学分明细查询后端 - Flask API
自动识别验证码，单接口查询
"""
import os
import base64
import json
import re
import time
import threading
import requests
import rsa
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

import uuid

app = Flask(__name__)
CORS(app)

# ============= 配置 =============
CAPTCHA_USERNAME = "yanglun123"
CAPTCHA_PASSWORD = "Yy123456"
DEFAULT_PASSWORD = "Aa@123456"
DEFAULT_YEAR = 2026
DEFAULT_CATEGORY = "国家级推荐项目"
BASE_URL = "https://hwonline.jumingedu.com"
MAX_QUERY_RETRIES = 3
RETRY_DELAYS = [5, 10, 30]
QUERY_TASK_TTL = 600  # 任务结果保留10分钟

CHAR_TABLE = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
PADDING_CHAR = '='

# ============= 异步任务存储 =============
query_tasks = {}  # task_id -> {status: 'running'|'done'|'error', result/error, time}


def retry_on_exception(max_retries=MAX_QUERY_RETRIES, delays=RETRY_DELAYS):
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = delays[attempt] if attempt < len(delays) else delays[-1]
                        time.sleep(wait_time)
                        continue
                    raise last_exception
            raise last_exception
        return wrapper
    return decorator


class CreditQueryEngine:
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            'accept': 'application/json, text/plain, */*',
            'authorization': 'null',
            'cache-control': 'no-cache',
            'client': 'PC',
            'content-type': 'application/x-www-form-urlencoded',
            'orgcode;': '',
            'origin': 'https://www.jumingedu.com',
            'pragma': 'no-cache',
            'referer': 'https://www.jumingedu.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

    def custom_base64_encode(self, hex_string):
        result = ''
        i = 0
        length = len(hex_string)
        while i + 3 <= length:
            chunk = hex_string[i:i + 3]
            num = int(chunk, 16)
            result += CHAR_TABLE[num >> 6 & 63]
            result += CHAR_TABLE[num & 63]
            i += 3
        remaining = length - i
        if remaining == 1:
            num = int(hex_string[i:i + 1], 16)
            result += CHAR_TABLE[num << 2 & 63]
            result += PADDING_CHAR * 2
        elif remaining == 2:
            num = int(hex_string[i:i + 2], 16)
            result += CHAR_TABLE[num >> 2 & 63]
            result += CHAR_TABLE[(num & 3) << 4 & 63]
            result += PADDING_CHAR
        return result

    def rsa_encrypt(self, public_key_pem, data):
        public_key = rsa.PublicKey.load_pkcs1_openssl_pem(public_key_pem.encode('utf-8'))
        encrypted_bytes = rsa.encrypt(data.encode('utf-8'), public_key)
        encrypted_hex = encrypted_bytes.hex()
        custom_encoded = self.custom_base64_encode(encrypted_hex)
        return base64.b64encode(custom_encoded.encode('latin-1')).decode('utf-8')

    def encrypt(self, key, data):
        public_key = f'-----BEGIN PUBLIC KEY-----\n    {key}\n    -----END PUBLIC KEY-----'
        return self.rsa_encrypt(public_key, data)

    @retry_on_exception()
    def get_key(self):
        url = f'{BASE_URL}/phrmacist/index/getPublicKey'
        resp = self.session.post(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @retry_on_exception()
    def get_captcha(self):
        url = f'{BASE_URL}/phrmacist/getpiccode'
        resp = self.session.post(url, headers=self.headers, data={'taken': ''}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @retry_on_exception()
    def solve_captcha(self, img_base64):
        """使用打码平台识别验证码"""
        data = {
            'username': CAPTCHA_USERNAME,
            'password': CAPTCHA_PASSWORD,
            'typeid': 3,
            'image': img_base64
        }
        resp = requests.post('http://api.ttshitu.com/predict', json=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result['success']:
            return result['data']['result']
        raise Exception(f"验证码识别失败: {result['message']}")

    @retry_on_exception()
    def login(self, username_enc, password_enc, captcha, pictaken):
        url = f'{BASE_URL}/phrmacist/index/login'
        data = {
            'username': username_enc,
            'password': password_enc,
            'yzm': captcha,
            'pictaken': pictaken
        }
        resp = self.session.post(url, headers=self.headers, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @retry_on_exception()
    def get_credits(self, authorization, page, year=DEFAULT_YEAR):
        headers = {
            'accept': 'application/json, text/plain, */*',
            'authorization': authorization,
            'cache-control': 'no-cache',
            'client': 'PC',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://www.jumingedu.com',
            'pragma': 'no-cache',
            'referer': 'https://www.jumingedu.com/UserIndex/creditQuery',
            'user-agent': 'Mozilla/5.0'
        }
        url = f'{BASE_URL}/phrmacist/userCenter/myExchange'
        data = {
            'year': year,
            'page': page,
            'rows': '5',
            'navigation': 'continueEdu01'
        }
        resp = requests.post(url, headers=headers, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def detect_field_mapping(self, sample_item):
        """自动检测字段映射"""
        mapping = {}
        field_names = list(sample_item.keys())

        # 项目编号
        for key in field_names:
            key_lower = key.lower()
            if any(k in key_lower for k in ['projectno', 'project_no', 'projno', 'no', 'number', 'code']):
                value = str(sample_item.get(key, ''))
                if re.search(r'\d{4}-\d{2}-\d{2}-\d{3}', value):
                    mapping['project_no'] = key
                    break

        # 项目名称
        for key in field_names:
            key_lower = key.lower()
            if any(k in key_lower for k in ['projectname', 'project_name', 'projname', 'name', 'title', 'subject']):
                value = str(sample_item.get(key, ''))
                if not re.search(r'\d{4}-\d{2}-\d{2}-\d{3}', value) and len(value) > 5:
                    mapping['project_name'] = key
                    break

        # 考核时间
        for key in field_names:
            key_lower = key.lower()
            if any(k in key_lower for k in ['gettime', 'get_time', 'time', 'examtime', 'exam_time', 'createtime']):
                value = str(sample_item.get(key, ''))
                if re.search(r'\d{4}-\d{2}-\d{2}', value):
                    mapping['exam_time'] = key
                    break

        # 获得学分
        for key in field_names:
            key_lower = key.lower()
            if any(k in key_lower for k in ['credit', 'score', 'credits', 'point']):
                value = sample_item.get(key)
                if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace('.', '').isdigit()):
                    mapping['score'] = key
                    break

        # 项目编号内嵌在名称中
        if 'project_no' not in mapping and 'project_name' in mapping:
            name_value = str(sample_item.get(mapping['project_name'], ''))
            if re.search(r'\d{4}-\d{2}-\d{2}-\d{3}', name_value):
                mapping['project_no_embedded'] = True

        return mapping

    def parse_item(self, item, mapping):
        result = {
            'course_name': '',
            'category': DEFAULT_CATEGORY,
            'score': 0,
            'exam_time': ''
        }

        if 'project_no' in mapping:
            result['project_no'] = str(item.get(mapping['project_no'], ''))

        if 'project_name' in mapping:
            name_value = str(item.get(mapping['project_name'], ''))
            if mapping.get('project_no_embedded'):
                match = re.search(r'(\d{4}-\d{2}-\d{2}-\d{3}[（(][^）)]+[）)])', name_value)
                if match:
                    result['project_no'] = match.group(1)
                    result['course_name'] = name_value.replace(match.group(1), '').strip()
                else:
                    result['course_name'] = name_value
            else:
                result['course_name'] = name_value

        if 'exam_time' in mapping:
            result['exam_time'] = str(item.get(mapping['exam_time'], ''))

        if 'score' in mapping:
            try:
                result['score'] = int(float(item.get(mapping['score'], 0)))
            except:
                result['score'] = 0

        return result

    def query(self, id_number, password=None, year=None, max_pages=10):
        """查询单个账号学分"""
        if password is None:
            password = DEFAULT_PASSWORD
        if year is None:
            year = DEFAULT_YEAR

        # 获取密钥
        key_res = self.get_key()
        key = key_res['result']['key']

        # 加密账号密码
        username_enc = self.encrypt(key, str(id_number))
        password_enc = self.encrypt(key, str(password))

        # 获取验证码并自动识别
        captcha_res = self.get_captcha()
        img_base64 = captcha_res['result']['pic'].split(',')[1]
        pictaken = captcha_res['result']['pictaken']
        captcha_text = self.solve_captcha(img_base64)

        # 登录
        login_attempts = 0
        token = None
        while login_attempts < 3:
            login_res = self.login(username_enc, password_enc, captcha_text, pictaken)

            if login_res['resultMsg'] in ('用户名或密码无效', '身份证或密码无效'):
                return {'success': False, 'error': '账号或密码错误'}

            if login_res['resultMsg'] == '验证码错误':
                login_attempts += 1
                if login_attempts < 3:
                    captcha_res = self.get_captcha()
                    img_base64 = captcha_res['result']['pic'].split(',')[1]
                    pictaken = captcha_res['result']['pictaken']
                    captcha_text = self.solve_captcha(img_base64)
                    continue

            if login_res['resultMsg'] is None and login_res['resultCode'] == '200':
                token = login_res['result']['token']
                break

            login_attempts += 1

        if not token:
            return {'success': False, 'error': '登录失败'}

        # 查询学分
        all_items = []
        mapping_detected = False
        field_mapping = {}

        for page in range(1, max_pages + 1):
            credit_res = self.get_credits(token, page, year)
            lists = credit_res.get('result', {}).get('data', {}).get('lists', [])

            if not lists:
                break

            if not mapping_detected and lists:
                field_mapping = self.detect_field_mapping(lists[0])
                mapping_detected = True

            for item in lists:
                parsed = self.parse_item(item, field_mapping)
                all_items.append(parsed)

            if len(lists) < 5:
                break

        total_credits = sum(item['score'] for item in all_items)

        return {
            'success': True,
            'items': all_items,
            'total_courses': len(all_items),
            'total_credits': total_credits
        }


# ============= API 路由 =============

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


def _run_query_task(task_id, id_number):
    """在后台线程中执行查询"""
    try:
        engine = CreditQueryEngine()
        result = engine.query(id_number)
        query_tasks[task_id] = {
            'status': 'done',
            'result': result,
            'time': time.time()
        }
    except Exception as e:
        query_tasks[task_id] = {
            'status': 'error',
            'error': f'查询异常: {str(e)}',
            'time': time.time()
        }


@app.route('/api/credit/query', methods=['POST'])
def query_credit():
    """提交查询任务，立即返回 task_id"""
    data = request.get_json()
    id_number = data.get('id_number', '').strip()

    if not id_number:
        return jsonify({'success': False, 'error': '请输入身份证号'})

    # 清理过期任务
    now = time.time()
    expired = [k for k, v in query_tasks.items() if now - v['time'] > QUERY_TASK_TTL]
    for k in expired:
        del query_tasks[k]

    # 生成任务ID并启动后台线程
    task_id = str(uuid.uuid4())[:8]
    query_tasks[task_id] = {'status': 'running', 'time': now}

    thread = threading.Thread(target=_run_query_task, args=(task_id, id_number), daemon=True)
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/credit/result/<task_id>', methods=['GET'])
def get_query_result(task_id):
    """轮询查询结果"""
    task = query_tasks.get(task_id)
    if not task:
        return jsonify({'status': 'not_found'})

    if task['status'] == 'running':
        return jsonify({'status': 'running'})
    elif task['status'] == 'done':
        return jsonify({'status': 'done', 'data': task['result']})
    else:
        return jsonify({'status': 'error', 'error': task.get('error', '未知错误')})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    app.run(host='0.0.0.0', port=port, debug=False)
