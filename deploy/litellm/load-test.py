#!/usr/bin/env python3
"""
LiteLLM 高并发压测脚本 —— 每个请求用不同的 prompt。

用法：
  export LITELLM_KEY=sk-xxx            # litellm 虚拟 Key 或 master key
  python3 load-test.py --n 200 --concurrency 20 --model sub2api-gpt-5.4

  # 流式：
  python3 load-test.py --n 100 -c 10 --stream

  # 换域名/模型：
  python3 load-test.py --base-url https://lumina.tripo3d.com --model gpt-5.4

统计：成功率、QPS、延迟分位(p50/p90/p99)、首字延迟(TTFB，流式)、token 用量、错误分类。
"""
import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time

import aiohttp

# 每个请求都不同的 prompt：模板 × 主题 × 序号，尽量避免命中缓存/粘性会话误合并。
TOPICS = [
    "量子计算", "深海生态", "古罗马历史", "分布式系统", "咖啡烘焙", "神经网络",
    "板块构造", "拜占庭艺术", "供应链金融", "光合作用", "密码学", "航天推进",
    "肠道菌群", "机器翻译", "城市规划", "期权定价", "蛋白质折叠", "音乐理论",
    "冰川消融", "边缘计算", "免疫系统", "半导体制程", "博弈论", "珊瑚白化",
]
TEMPLATES = [
    "用一句话解释{t}的核心概念。",
    "列出关于{t}的三个常见误解。",
    "写一个关于{t}的两句话的比喻。",
    "{t}领域最近十年最重要的进展是什么？简短回答。",
    "如果要向十岁小孩解释{t}，你会怎么说？",
    "给出{t}的一个反直觉的事实。",
    "用中文写一句关于{t}的俳句。",
    "{t}和日常生活有什么意想不到的联系？一句话。",
]


def make_prompt(i: int) -> str:
    t = random.choice(TOPICS)
    tmpl = random.choice(TEMPLATES)
    # 加序号 + 随机盐，确保每条 prompt 唯一
    return f"[req#{i}·{random.randint(1000,9999)}] " + tmpl.format(t=t)


class Stats:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.latencies = []      # 完整请求耗时
        self.ttfbs = []          # 首字延迟（流式）
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.errors = {}         # 错误分类计数

    def record_err(self, key):
        self.errors[key] = self.errors.get(key, 0) + 1


async def one_request(session, sem, url, headers, model, idx, stream, timeout, stats, verbose):
    prompt = make_prompt(idx)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 64,
        "stream": stream,
        # 带稳定会话标识，走 sub2api 请求体粘性主路径（每请求独立会话）
        "metadata": {"user_id": f"session_loadtest_{idx}"},
    }
    async with sem:
        t0 = time.perf_counter()
        ttfb = None
        try:
            async with session.post(url, headers=headers, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    stats.fail += 1
                    stats.record_err(f"HTTP {resp.status}")
                    if verbose:
                        print(f"[{idx}] HTTP {resp.status}: {body}", file=sys.stderr)
                    return
                if stream:
                    async for line in resp.content:
                        if ttfb is None and line.strip():
                            ttfb = time.perf_counter() - t0
                            stats.ttfbs.append(ttfb)
                    # 流式不易取 usage，跳过 token 统计
                else:
                    data = await resp.json()
                    usage = data.get("usage", {}) or {}
                    stats.prompt_tokens += usage.get("prompt_tokens", 0)
                    stats.completion_tokens += usage.get("completion_tokens", 0)
                dt = time.perf_counter() - t0
                stats.latencies.append(dt)
                stats.ok += 1
                if verbose:
                    tag = f" ttfb={ttfb*1000:.0f}ms" if ttfb else ""
                    print(f"[{idx}] ok {dt*1000:.0f}ms{tag}")
        except asyncio.TimeoutError:
            stats.fail += 1
            stats.record_err("timeout")
            if verbose:
                print(f"[{idx}] timeout", file=sys.stderr)
        except Exception as e:
            stats.fail += 1
            stats.record_err(type(e).__name__)
            if verbose:
                print(f"[{idx}] {type(e).__name__}: {e}", file=sys.stderr)


def pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round((p / 100) * (len(vals) - 1)))))
    return vals[k]


async def main():
    ap = argparse.ArgumentParser(description="LiteLLM 高并发压测（每请求不同 prompt）")
    ap.add_argument("--base-url", default=os.environ.get("LITELLM_BASE_URL", "https://lumina.tripo3d.com"),
                    help="litellm 域名（默认 https://lumina.tripo3d.com）")
    ap.add_argument("--model", default="sub2api-gpt-5.4", help="model_name")
    ap.add_argument("--n", type=int, default=100, help="总请求数")
    ap.add_argument("-c", "--concurrency", type=int, default=20, help="并发度")
    ap.add_argument("--stream", action="store_true", help="流式请求（统计 TTFB）")
    ap.add_argument("--timeout", type=float, default=120, help="单请求超时(秒)")
    ap.add_argument("--key", default=os.environ.get("LITELLM_KEY", ""), help="litellm Key（或用 LITELLM_KEY 环境变量）")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = ap.parse_args()

    if not args.key:
        print("ERROR: 需要 litellm Key。用 --key 或 export LITELLM_KEY=sk-xxx", file=sys.stderr)
        sys.exit(2)

    url = args.base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"}
    stats = Stats()
    sem = asyncio.Semaphore(args.concurrency)

    print(f"目标: {url}")
    print(f"模型: {args.model} | 请求数: {args.n} | 并发: {args.concurrency} | 流式: {args.stream}")
    print("-" * 60)

    connector = aiohttp.TCPConnector(limit=args.concurrency, ssl=True)
    t_start = time.perf_counter()
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            one_request(session, sem, url, headers, args.model, i, args.stream,
                        args.timeout, stats, args.verbose)
            for i in range(args.n)
        ]
        await asyncio.gather(*tasks)
    wall = time.perf_counter() - t_start

    lat_ms = [x * 1000 for x in stats.latencies]
    print("-" * 60)
    print(f"总耗时:      {wall:.2f}s")
    print(f"成功/失败:   {stats.ok} / {stats.fail}  (成功率 {100*stats.ok/max(1,args.n):.1f}%)")
    print(f"吞吐 QPS:    {stats.ok/wall:.1f}")
    if lat_ms:
        print(f"延迟(ms):    p50={pct(lat_ms,50):.0f}  p90={pct(lat_ms,90):.0f}  "
              f"p99={pct(lat_ms,99):.0f}  max={max(lat_ms):.0f}  avg={statistics.mean(lat_ms):.0f}")
    if stats.ttfbs:
        ttfb_ms = [x * 1000 for x in stats.ttfbs]
        print(f"首字TTFB(ms):p50={pct(ttfb_ms,50):.0f}  p90={pct(ttfb_ms,90):.0f}  p99={pct(ttfb_ms,99):.0f}")
    if stats.prompt_tokens or stats.completion_tokens:
        print(f"Token:       prompt={stats.prompt_tokens}  completion={stats.completion_tokens}")
    if stats.errors:
        print(f"错误分类:    {json.dumps(stats.errors, ensure_ascii=False)}")


if __name__ == "__main__":
    asyncio.run(main())
