#!/usr/bin/env python3
"""
sub2api GPT 模型 —— 通过 litellm 的端到端 QPS 阶梯压测。

对每个模型逐档提升并发，找出：
  - 100% 可用 QPS：最后一档成功率 == 100% 的实测 QPS
  - 最大 QPS：所有档位里实测吞吐的峰值（可能伴随少量失败）

链路：客户端 -> litellm(https 域名) -> sub2api -> 上游 ChatGPT 账号池。
每请求 prompt 唯一，避免缓存/粘性会话误合并。

用法：
  export LITELLM_KEY=sk-xxx
  python3 qps-ramp.py                       # 全部已配模型，默认阶梯
  python3 qps-ramp.py --models sub2api-gpt-5.4 sub2api-gpt-5.5
  python3 qps-ramp.py --steps 5 10 20 30 40 --per-step 40
  python3 qps-ramp.py --stop-success 0.9    # 某档成功率 < 90% 即停止加压

输出：每模型一张阶梯表 + 结论（100% QPS / 最大 QPS）。
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

TOPICS = [
    "量子计算", "深海生态", "古罗马史", "分布式系统", "咖啡烘焙", "神经网络",
    "板块构造", "拜占庭艺术", "供应链金融", "光合作用", "密码学", "航天推进",
    "肠道菌群", "机器翻译", "城市规划", "期权定价", "蛋白质折叠", "音乐理论",
    "冰川消融", "边缘计算", "免疫系统", "半导体制程", "博弈论", "珊瑚白化",
]
TEMPLATES = [
    "用一句话解释{t}的核心概念。",
    "列出关于{t}的三个常见误解。",
    "写一个关于{t}的两句话比喻。",
    "{t}最近十年最重要的进展是什么？简短回答。",
    "如果向十岁小孩解释{t}，你会怎么说？",
    "给出{t}的一个反直觉事实。",
    "{t}和日常生活有什么意想不到的联系？一句话。",
]

# 全部通过探测的 sub2api 可用模型（litellm 里的 model_name）
DEFAULT_MODELS = [
    "sub2api-gpt-5.4",
    "sub2api-gpt-5.4-mini",
    "sub2api-gpt-5.5",
    "sub2api-gpt-5.6",
    "sub2api-gpt-5.6-luna",
    "sub2api-gpt-5.6-sol",
    "sub2api-gpt-5.6-terra",
    "sub2api-codex-auto-review",
]
DEFAULT_STEPS = [1, 5, 10, 20, 30, 40, 60]


def make_prompt(i):
    t = random.choice(TOPICS)
    return f"[r{i}·{random.randint(1000,9999)}] " + random.choice(TEMPLATES).format(t=t)


async def one_req(session, sem, url, headers, model, idx, timeout, out):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": make_prompt(idx)}],
        "max_tokens": 48,
        "stream": False,
        "metadata": {"user_id": f"session_ramp_{model}_{idx}"},
    }
    async with sem:
        t0 = time.perf_counter()
        try:
            async with session.post(url, headers=headers, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                body = await r.read()
                dt = time.perf_counter() - t0
                if r.status == 200 and b'"content"' in body:
                    out["lat"].append(dt)
                    out["ok"] += 1
                else:
                    out["fail"] += 1
                    key = f"HTTP {r.status}"
                    out["err"][key] = out["err"].get(key, 0) + 1
        except asyncio.TimeoutError:
            out["fail"] += 1
            out["err"]["timeout"] = out["err"].get("timeout", 0) + 1
        except Exception as e:
            out["fail"] += 1
            k = type(e).__name__
            out["err"][k] = out["err"].get(k, 0) + 1


def pctl(v, p):
    if not v:
        return 0.0
    v = sorted(v)
    return v[max(0, min(len(v) - 1, int(round(p / 100 * (len(v) - 1)))))]


async def run_step(session, url, headers, model, concurrency, count, timeout):
    out = {"ok": 0, "fail": 0, "lat": [], "err": {}}
    sem = asyncio.Semaphore(concurrency)
    t0 = time.perf_counter()
    await asyncio.gather(*[
        one_req(session, sem, url, headers, model, i, timeout, out)
        for i in range(count)
    ])
    wall = time.perf_counter() - t0
    total = out["ok"] + out["fail"]
    return {
        "concurrency": concurrency,
        "count": count,
        "ok": out["ok"],
        "fail": out["fail"],
        "success": out["ok"] / max(1, total),
        "qps": out["ok"] / wall if wall > 0 else 0,
        "wall": wall,
        "p50": pctl(out["lat"], 50) * 1000,
        "p90": pctl(out["lat"], 90) * 1000,
        "p99": pctl(out["lat"], 99) * 1000,
        "err": out["err"],
    }


async def ramp_model(session, url, headers, model, steps, per_step, timeout, stop_success, cooldown):
    print(f"\n{'='*72}\n模型: {model}\n{'='*72}")
    print(f"{'并发':>4} {'请求':>4} {'成功':>5} {'失败':>4} {'成功率':>7} {'QPS':>6} "
          f"{'p50ms':>7} {'p90ms':>7} {'p99ms':>7}  错误")
    rows = []
    for c in steps:
        n = max(per_step, c)  # 每档至少发满一轮并发
        r = await run_step(session, url, headers, model, c, n, timeout)
        rows.append(r)
        errstr = json.dumps(r["err"], ensure_ascii=False) if r["err"] else ""
        print(f"{c:>4} {n:>4} {r['ok']:>5} {r['fail']:>4} {r['success']*100:>6.1f}% "
              f"{r['qps']:>6.2f} {r['p50']:>7.0f} {r['p90']:>7.0f} {r['p99']:>7.0f}  {errstr}",
              flush=True)
        if r["success"] < stop_success:
            print(f"  ↳ 成功率 {r['success']*100:.1f}% < {stop_success*100:.0f}%，停止加压（保护上游）")
            break
        await asyncio.sleep(cooldown)

    full = [r for r in rows if r["success"] >= 0.999]
    qps100 = max((r["qps"] for r in full), default=0.0)
    c100 = max((r["concurrency"] for r in full), default=0)
    qpsmax = max((r["qps"] for r in rows), default=0.0)
    cmax = max((r["concurrency"] for r in rows if r["qps"] == qpsmax), default=0)
    return {
        "model": model, "rows": rows,
        "qps100": qps100, "c100": c100,
        "qpsmax": qpsmax, "cmax": cmax,
    }


async def main():
    ap = argparse.ArgumentParser(description="sub2api GPT 模型端到端 QPS 阶梯压测（经 litellm）")
    ap.add_argument("--base-url", default=os.environ.get("LITELLM_BASE_URL", "https://lumina.tripo3d.com"))
    ap.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--steps", nargs="*", type=int, default=DEFAULT_STEPS, help="并发阶梯")
    ap.add_argument("--per-step", type=int, default=40, help="每档请求数（<并发时按并发）")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--stop-success", type=float, default=0.8, help="成功率低于此值停止加压")
    ap.add_argument("--cooldown", type=float, default=3, help="档位间冷却秒数")
    ap.add_argument("--key", default=os.environ.get("LITELLM_KEY", ""))
    args = ap.parse_args()

    if not args.key:
        print("ERROR: 需要 litellm Key。export LITELLM_KEY=sk-xxx 或 --key", file=sys.stderr)
        sys.exit(2)

    url = args.base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"}
    print(f"目标: {url}")
    print(f"模型数: {len(args.models)} | 阶梯: {args.steps} | 每档请求: {args.per_step} | "
          f"stop@success<{args.stop_success*100:.0f}%")

    connector = aiohttp.TCPConnector(limit=max(args.steps) + 5, ssl=True)
    summaries = []
    async with aiohttp.ClientSession(connector=connector) as session:
        for m in args.models:
            summaries.append(
                await ramp_model(session, url, headers, m, args.steps,
                                 args.per_step, args.timeout, args.stop_success, args.cooldown)
            )

    print(f"\n{'='*72}\n汇总（每模型）\n{'='*72}")
    print(f"{'模型':<28} {'100%可用QPS':>12} {'(并发)':>7} {'最大QPS':>9} {'(并发)':>7}")
    for s in summaries:
        print(f"{s['model']:<28} {s['qps100']:>12.2f} {s['c100']:>7} {s['qpsmax']:>9.2f} {s['cmax']:>7}")
    agg100 = sum(s["qps100"] for s in summaries)
    aggmax = sum(s["qpsmax"] for s in summaries)
    print(f"{'-'*72}")
    print(f"{'合计(各模型独立测得)':<28} {agg100:>12.2f} {'':>7} {aggmax:>9.2f}")
    print("\n说明：QPS 为该并发档实测吞吐；'100%可用QPS' 取成功率=100% 档中的最大吞吐；")
    print("     '最大QPS' 取所有档峰值（可能已伴随失败）。上游账号有限，勿长时间高并发。")


if __name__ == "__main__":
    asyncio.run(main())
