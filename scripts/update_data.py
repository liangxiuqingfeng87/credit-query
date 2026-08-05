#!/usr/bin/env python3
"""
GitHub Actions 自动更新脚本：从腾讯文档「举名新表」拉取数据，生成 query_compact.json，推送到 GitHub。
每日 8:00 北京时间（UTC+0）自动执行。
"""
import csv
import io
import json
import os
import re
import sys
import time
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ===================== 配置 =====================
DOC_URL = "https://docs.qq.com/sheet/DY0FRekZWRmVhZU9P"
DOC_ID = "DY0FRekZWRmVhZU9P"          # 从 URL 提取的文档 ID
GITHUB_REPO = "liangxiuqingfeng87/credit-query"
GITHUB_BRANCH = "main"

COOKIE = os.environ.get("TENCENT_DOCS_COOKIE", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# 腾讯文档导出 API
EXPORT_URL = "https://docs.qq.com/v1/export/export_office"
PROGRESS_URL = "https://docs.qq.com/v1/export/query_progress"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://docs.qq.com/",
    "Origin": "https://docs.qq.com",
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_doc_info(cookie):
    """获取文档信息，包括正确的 internal docId"""
    url = f"https://docs.qq.com/dop-api/opendoc?id={DOC_ID}&normal=1"
    h = {**HEADERS, "Cookie": cookie}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # 提取 padId 和 domainId（用于导出 API）
    client_vars = data.get("clientVars", {})
    pad_id = client_vars.get("padId", "")
    domain_id = client_vars.get("domainId", "")
    internal_doc_id = f"{domain_id}${pad_id}" if domain_id and pad_id else DOC_ID

    # 找到「举名新表」的 sheet id
    collab_vars = data.get("collab_client_vars", {})
    sheet_list = collab_vars.get("header", [])
    target_sheet_id = None
    for s in sheet_list:
        if "举名新表" in s.get("name", ""):
            target_sheet_id = s.get("id", "")
            break

    log(f"Internal docId: {internal_doc_id}")
    log(f"Target sheet: {target_sheet_id}")
    return internal_doc_id, target_sheet_id


def export_sheet_as_csv(cookie, internal_doc_id, sheet_id):
    """导出表格为 CSV 格式并返回内容"""
    # Step 1: 发起导出请求
    export_data = {
        "padId": internal_doc_id.split("$")[-1] if "$" in internal_doc_id else internal_doc_id,
        "domainId": internal_doc_id.split("$")[0] if "$" in internal_doc_id else "300000000",
        "exportType": 1,  # 1 = CSV
    }

    # 尝试使用 x-www-form-urlencoded
    encoded_data = urllib.parse.urlencode(export_data).encode()
    h = {**HEADERS, "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(EXPORT_URL, data=encoded_data, headers=h, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"Export request failed: {e.code} - {body[:500]}")
        raise

    operation_id = result.get("operationId", "")
    if not operation_id:
        log(f"Export response: {json.dumps(result, ensure_ascii=False)[:500]}")
        raise Exception("No operationId in export response")

    log(f"Export started: operationId={operation_id}")

    # Step 2: 轮询导出进度
    max_wait = 120
    for i in range(max_wait // 2):
        time.sleep(2)
        progress_data = urllib.parse.urlencode({"operationId": operation_id}).encode()
        req = urllib.request.Request(PROGRESS_URL, data=progress_data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                progress = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            log(f"Progress check failed: {e.code}")
            continue

        status = progress.get("type", "")
        if status == "success":
            download_url = progress.get("url", "")
            log(f"Export ready: {download_url[:80]}...")
            # Step 3: 下载 CSV
            req = urllib.request.Request(download_url, headers={**HEADERS, "Cookie": cookie})
            with urllib.request.urlopen(req, timeout=60) as resp:
                csv_content = resp.read()
            # 尝试解码
            for encoding in ["utf-8", "gbk", "gb2312", "gb18030"]:
                try:
                    return csv_content.decode(encoding)
                except (UnicodeDecodeError, LookupError):
                    continue
            return csv_content.decode("utf-8", errors="replace")

        elif status == "error":
            log(f"Export failed: {json.dumps(progress, ensure_ascii=False)[:300]}")
            raise Exception("Export failed")
        else:
            log(f"  Waiting... (status={status})")

    raise Exception("Export timed out")


def parse_csv_to_entries(csv_text):
    """将 CSV 数据按身份证号去重合并，返回 entries 字典"""
    # 列: 0姓名, 1性别, 2手机号, 3身份证号, 4单位, 5省份, 6市/州, 7区/县, 8学科, 9所需分值, 10是否中医学分, 11是否处理, 12查得分值, 13判断是否上分
    reader = csv.reader(io.StringIO(csv_text))
    all_entries = {}
    total_rows = 0

    for row in reader:
        if not row or len(row) < 14:
            continue
        name = row[0].strip()
        id_num = row[3].strip()
        unit = row[4].strip()
        province = row[5].strip()
        city = row[6].strip()
        district = row[7].strip() if len(row) > 7 else ""
        subject = row[8].strip() if len(row) > 8 else ""
        required = row[9].strip() if len(row) > 9 else ""
        scored = row[12].strip() if len(row) > 12 else ""
        judgment = row[13].strip() if len(row) > 13 else ""

        if not name or not id_num:
            continue

        total_rows += 1
        if id_num not in all_entries:
            all_entries[id_num] = {
                "name": name,
                "id": id_num,
                "unit": unit,
                "province": province,
                "city": city,
                "district": district,
                "subjects": [],
                "total_required": 0,
                "total_scored": 0,
                "judgments": set(),
            }

        e = all_entries[id_num]
        req_val = int(required) if required and required.isdigit() else 0
        scr_val = int(scored) if scored and scored.isdigit() else 0
        e["subjects"].append({"s": subject, "r": req_val, "sc": scr_val})
        e["total_required"] += req_val
        e["total_scored"] += scr_val
        if judgment:
            e["judgments"].add(judgment)

    log(f"Parsed {total_rows} rows, {len(all_entries)} unique people")
    return all_entries


def build_compact_json(entries):
    """将 entries 转为紧凑 JSON 格式"""
    compact = []
    for e in entries.values():
        judgments = list(e["judgments"])
        done = all(j == "已上分" for j in judgments) if judgments else False
        gap = e["total_required"] - e["total_scored"]

        # 备注取第一个非"已上分"的判断
        j_str = ""
        if not done:
            for j in judgments:
                if j != "已上分":
                    j_str = j
                    break
            if not j_str and judgments:
                j_str = judgments[0]

        compact.append(
            {
                "n": e["name"],
                "i": e["id"],
                "u": e["unit"],
                "p": e["province"],
                "c": e["city"],
                "d": e["district"],
                "tr": e["total_required"],
                "ts": e["total_scored"],
                "g": gap,
                "dn": done,
                "j": j_str,
                "s": [
                    {"sn": s["s"], "r": s["r"], "sc": s["sc"]} for s in e["subjects"]
                ],
            }
        )

    # 按身份证号排序
    compact.sort(key=lambda x: x["i"])
    return compact


def push_to_github(filename, content_bytes, message):
    """通过 GitHub API 推送文件"""
    api_base = f"https://api.github.com/repos/{GITHUB_REPO}"
    h = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "credit-query-bot",
    }

    # 获取当前文件 SHA
    url = f"{api_base}/contents/{filename}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(url, headers=h)
    sha = None
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            sha = json.loads(resp.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError:
        pass  # 文件不存在

    # 构建请求
    data = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        data["sha"] = sha

    req = urllib.request.Request(
        f"{api_base}/contents/{filename}",
        data=json.dumps(data).encode("utf-8"),
        headers={**h, "Content-Type": "application/json"},
        method="PUT",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        log(f"Pushed {filename}: {result.get('content', {}).get('sha', 'ok')[:12]}")


# ===================== 主流程 =====================
def main():
    log("=== 开始每日学分数据更新 ===")

    if not COOKIE:
        log("ERROR: 未设置 TENCENT_DOCS_COOKIE 环境变量")
        sys.exit(1)

    if not GITHUB_TOKEN:
        log("ERROR: 未设置 GITHUB_TOKEN")
        sys.exit(1)

    # 1. 获取文档信息
    log("Step 1: 获取腾讯文档信息...")
    internal_doc_id, sheet_id = get_doc_info(COOKIE)
    if not internal_doc_id:
        log("ERROR: 无法获取文档信息")
        sys.exit(1)

    # 2. 导出 CSV
    log("Step 2: 导出表格为 CSV...")
    csv_text = export_sheet_as_csv(COOKIE, internal_doc_id, sheet_id)
    lines = csv_text.count("\n")
    log(f"CSV 下载完成: {lines} 行, {len(csv_text)} 字节")

    # 3. 解析并去重
    log("Step 3: 解析数据...")
    entries = parse_csv_to_entries(csv_text)

    # 4. 生成紧凑 JSON
    log("Step 4: 生成 query_compact.json...")
    compact = build_compact_json(entries)
    json_bytes = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    size_mb = len(json_bytes) / 1024 / 1024

    # 5. 统计
    done_count = sum(1 for p in compact if p["dn"])
    not_done = len(compact) - done_count

    # 6. 推送到 GitHub
    log("Step 5: 推送到 GitHub...")
    beijing_tz = timezone(timedelta(hours=8))
    now_str = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M")
    push_to_github("query_compact.json", json_bytes, f"Auto update: {now_str} - {len(compact)}人, 已达标{done_count}, 差分{not_done}")

    # 7. 输出报告
    log("=== 更新完成 ===")
    log(f"总人数: {len(compact)}")
    log(f"已达标: {done_count}")
    log(f"差分:   {not_done}")
    log(f"文件大小: {size_mb:.1f} MB")
    log(f"更新时间: {now_str} 北京时间")


if __name__ == "__main__":
    main()
