# -*- coding: utf-8 -*-
# 参考文档：https://quantapi.51ifind.com
# 参考文档：https://quantapi.51ifind.com/api/v1/get_access_token
import os
import sys
import time
import datetime
import pandas as pd

# ================= 配置区域 =================
START_DATE = "2020-01-01"           # 抓取历史行情的开始日期
OUTPUT_FILE = "ashare_history_data.csv"  # 数据保存路径
PROGRESS_FILE = "download_progress.txt"  # 进度记录文件路径
FAILED_FILE = "download_failed.txt"      # 彻底失败的股票记录文件
BATCH_SIZE = 50                     # 每次请求的股票数量（如果整批失败，会自动启动二分降级下载）
REQUEST_DELAY = 0.5                  # 每次请求的间隔时间（秒），防止被接口限流
# ============================================

def load_env(filepath=".env"):
    """轻量级加载 .env 配置文件，避免第三方库依赖"""
    if not os.path.exists(filepath):
        print(f"提示：未找到 {filepath} 文件，将使用系统环境变量。")
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip()

def load_stock_codes():
    """从同级目录下的 stock_codes.txt 加载所有股票代码"""
    file_path = "stock_codes.txt"
    if not os.path.exists(file_path):
        print(f"错误: 未找到 {file_path} 文件！")
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    # 兼容英文逗号、中文逗号和换行符分隔的代码
    codes = [c.strip() for c in content.replace("，", ",").replace("\n", ",").split(",") if c.strip()]
    return codes

def load_progress():
    """读取已完成下载的股票代码列表"""
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_progress(codes, progress_file):
    """记录已成功下载/处理的股票代码"""
    with open(progress_file, "a", encoding="utf-8") as f:
        for code in codes:
            f.write(f"{code}\n")

def save_failed(code, failed_file):
    """记录彻底下载失败的股票代码"""
    with open(failed_file, "a", encoding="utf-8") as f:
        f.write(f"{code}\n")

def fetch_with_retry(batch_codes, start_date, end_date, max_retries=3):
    """
    带重试机制的单次数据获取
    """
    from iFinDPy import THS_HD
    codes_str = ",".join(batch_codes)
    for attempt in range(max_retries):
        try:
            # 历史行情-开盘价;最高价;最低价;收盘价;均价;成交量-iFinD数据接口
            data = THS_HD(codes_str, 'open;high;low;close;vwap;volume', '', start_date, end_date)
            if data.errorcode == 0:
                return data.data
            else:
                print(f" -> 接口返回错误 (尝试 {attempt+1}/{max_retries}): 错误码 {data.errorcode}, 错误信息: {data.errmsg}")
        except Exception as e:
            print(f" -> 网络或服务异常 (尝试 {attempt+1}/{max_retries}): {e}")
        
        if attempt < max_retries - 1:
            time.sleep(3)
    return None

def process_dataframe(df):
    """
    规范化返回的 DataFrame：统一列名小写、转换数值类型并排序
    """
    df = df.copy()
    # 统一列名
    rename_cols = {}
    for col in df.columns:
        col_lower = col.lower()
        if col_lower in ['thscode', 'code', 'symbol']:
            rename_cols[col] = 'thscode'
        elif col_lower in ['time', 'date']:
            rename_cols[col] = 'time'
        else:
            rename_cols[col] = col_lower
    df.rename(columns=rename_cols, inplace=True)
    
    # 转换数值类型，保持原始精度
    for col in ['open', 'high', 'low', 'close', 'vwap']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
    if 'volume' in df.columns:
        # volume 转换为整型（支持 NaN 格式的整型 Int64）
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('Int64')
        
    # 按 thscode 升序，time 升序排序，使之符合时间序列分析习惯
    sort_cols = []
    sort_asc = []
    if 'thscode' in df.columns:
        sort_cols.append('thscode')
        sort_asc.append(True)
    if 'time' in df.columns:
        sort_cols.append('time')
        sort_asc.append(True)
    if sort_cols:
        df.sort_values(by=sort_cols, ascending=sort_asc, inplace=True)
        
    # 调整列顺序，将 time 放在最前，thscode 放在第二
    target_cols = ['time', 'thscode', 'open', 'high', 'low', 'close', 'vwap', 'volume']
    ordered_cols = [col for col in target_cols if col in df.columns]
    for col in df.columns:
        if col not in ordered_cols:
            ordered_cols.append(col)
    df = df[ordered_cols]
    
    # 去除价格全部缺失的无效行（避免空数据占空间）
    df.dropna(subset=['open', 'high', 'low', 'close'], how='all', inplace=True)
    return df

def write_to_csv(df, filepath):
    """
    将 DataFrame 追加保存到 CSV 文件
    """
    file_exists = os.path.exists(filepath)
    df.to_csv(filepath, mode='a', header=not file_exists, index=False, encoding="utf-8-sig")

def download_batch_recursive(batch_codes, start_date, end_date):
    """
    递归下载股票数据。若整组失败，则使用二分法拆分下载，直到单只股票彻底失败并记录跳过。
    """
    df = fetch_with_retry(batch_codes, start_date, end_date)
    if df is not None:
        if not df.empty:
            cleaned_df = process_dataframe(df)
            if not cleaned_df.empty:
                write_to_csv(cleaned_df, OUTPUT_FILE)
                print(f" -> 成功追加写入 {len(cleaned_df)} 条记录。")
        else:
            print(" -> 接口返回空数据。")
        save_progress(batch_codes, PROGRESS_FILE)
        return True
    
    # 整批下载彻底失败，且无法通过重试解决
    if len(batch_codes) <= 1:
        failed_stock = batch_codes[0]
        print(f"警告：股票 {failed_stock} 单独请求依然失败，跳过。")
        save_failed(failed_stock, FAILED_FILE)
        save_progress(batch_codes, PROGRESS_FILE)  # 标记已处理，防止陷入死循环
        return False
    
    # 启动二分折半降级策略
    mid = len(batch_codes) // 2
    part1 = batch_codes[:mid]
    part2 = batch_codes[mid:]
    print(f" -> 批次下载失败，正在折半拆分下载：拆分为 {len(part1)} 和 {len(part2)} 只股票进行请求。")
    download_batch_recursive(part1, start_date, end_date)
    download_batch_recursive(part2, start_date, end_date)
    return True

def main():
    # 1. 初始化环境变量
    load_env()
    username = os.getenv("IFIND_USER")
    password = os.getenv("IFIND_PASSWORD")

    if not username or not password or username == "your_username" or password == "your_password":
        print("错误：请先在 .env 文件中配置您的 iFinD 账号和密码 (IFIND_USER 和 IFIND_PASSWORD)！")
        sys.exit(1)

    # 2. 获取目标股票代码列表并处理进度
    all_stocks = load_stock_codes()
    if not all_stocks:
        print("错误：未能在 stock_codes.txt 中加载到任何股票代码。")
        return
        
    downloaded = load_progress()
    pending_stocks = [s for s in all_stocks if s not in downloaded]
    
    total_count = len(all_stocks)
    downloaded_count = len(downloaded)
    pending_count = len(pending_stocks)
    
    print(f"=== iFinD 全量日行情数据爬取 (2020年至今) ===")
    print(f"总股票数: {total_count}")
    print(f"已下载数: {downloaded_count}")
    print(f"剩余下载: {pending_count}")
    
    if pending_count == 0:
        print("所有股票已下载完毕，无需重复抓取。")
        return

    # 3. 动态获取当前日期作为抓取截止日期
    end_date = datetime.date.today().strftime("%Y-%m-%d")
    print(f"抓取时间段: {START_DATE} 至 {end_date}")

    # 4. 导入 iFinD SDK 并登录
    try:
        from iFinDPy import THS_iFinDLogin, THS_iFinDLogout
    except ImportError:
        print("\n错误：导入 iFinDPy 失败！请确保在同花顺 iFinD 客户端的超级命令(sc)中配置了 Python 环境插件。")
        sys.exit(1)

    print("正在连接 iFinD 数据接口并登录...")
    login_status = THS_iFinDLogin(username, password)
    if login_status != 0 and login_status != -201:
        print(f"登录失败，错误码：{login_status}。请确认账号密码无误且未在其他电脑重复登录。")
        sys.exit(1)
    print("登录成功！")

    # 5. 分批循环下载
    try:
        total_batches = (pending_count + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, pending_count, BATCH_SIZE):
            batch_num = i // BATCH_SIZE + 1
            batch_codes = pending_stocks[i : i + BATCH_SIZE]
            
            print(f"\n[{batch_num}/{total_batches}] 正在抓取包含 {len(batch_codes)} 只股票的批次...")
            
            # 执行带有二分降级下载策略的递归抓取
            download_batch_recursive(batch_codes, START_DATE, end_date)
            
            # 防频限流延迟
            if batch_num < total_batches:
                time.sleep(REQUEST_DELAY)
                
    except KeyboardInterrupt:
        print("\n[Ctrl+C] 抓取进程被用户主动终止。已成功保存进度，下次运行将自动断点续传。")
    finally:
        # 6. 注销登录
        print("\n正在安全断开 iFinD 接口连接并退出登录...")
        THS_iFinDLogout()
        print("已成功退出登录。")
        
        # 再次汇总
        final_downloaded = load_progress()
        print(f"\n抓取任务阶段性完成！当前总抓取进度: {len(final_downloaded)}/{total_count}。")

if __name__ == "__main__":
    main()
