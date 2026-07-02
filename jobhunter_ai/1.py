import requests

url = "https://mcs.zijieapi.com/list?aid=7497&sdk_version=5.1.24_dy"
headers = {
  "Host": "mcs.zijieapi.com",
  "Connection": "keep-alive",
  "sec-ch-ua-platform": "\"Windows\"",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
  "sec-ch-ua": "\"Microsoft Edge\";v=\"149\", \"Chromium\";v=\"149\", \"Not)A;Brand\";v=\"24\"",
  "Content-Type": "application/json; charset=UTF-8",
  "sec-ch-ua-mobile": "?0",
  "Accept": "*/*",
  "Origin": "https://www.douyin.com",
  "Sec-Fetch-Site": "cross-site",
  "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Dest": "empty",
  "Referer": "https://www.douyin.com/",
  "Accept-Encoding": "gzip, deflate, br, zstd",
  "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6"
}
data = """[{\"events\":[{\"event\":\"_be_active\",\"params\":\"{\\\"start_time\\\":1782982624272,\\\"end_time\\\":1782982629968,\\\"url\\\":\\\"https://www.douyin.com/?recommend=1\\\",\\\"referrer\\\":\\\"https://ntp.msn.cn/\\\",\\\"title\\\":\\\"的抖音直播间 - 抖音直播\\\",\\\"event_index\\\":1782983145303}\",\"local_time_ms\":1782982684273,\"is_bav\":0,\"session_id\":\"5298b0c0-a255-4d24-b6b8-c1f40887ea80\"}],\"user\":{\"user_unique_id\":\"7566971228150957614\",\"device_id\":\"7566971228150957614\",\"web_id\":\"7566971229530621503\"},\"header\":{\"app_id\":7497,\"os_name\":\"windows\",\"os_version\":\"10\",\"device_model\":\"Windows NT 10.0\",\"language\":\"zh-CN\",\"platform\":\"web\",\"sdk_version\":\"5.1.24_dy\",\"sdk_lib\":\"js\",\"timezone\":8,\"tz_offset\":-28800,\"resolution\":\"1920x1080\",\"browser\":\"Microsoft Edge\",\"browser_version\":\"149.0.0.0\",\"referrer\":\"https://ntp.msn.cn/\",\"referrer_host\":\"ntp.msn.cn\",\"width\":1920,\"height\":1080,\"screen_width\":1920,\"screen_height\":1080,\"tracer_data\":\"{\\\"$utm_from_url\\\":1}\",\"custom\":\"{\\\"$latest_referrer\\\":\\\"https://ntp.msn.cn/\\\",\\\"$latest_referrer_host\\\":\\\"ntp.msn.cn\\\",\\\"$latest_search_keyword\\\":\\\"\\\"}\"},\"local_time\":1782982684,\"verbose\":1}]"""

res = requests.post(url, headers=headers, data=data)
print(res.text)
