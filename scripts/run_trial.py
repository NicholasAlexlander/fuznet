#!/usr/bin/env python3
"""Run a bounded fuznet trial and keep each worker's results separate."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VIVADO = "/opt/Xilinx/Vivado/2024.2/bin/vivado"


def read_seeds(path):
    with path.open(newline="") as handle:
        first = handle.readline().strip()
        handle.seek(0)
        if first.startswith("timestamp,"):
            seeds = [row["seed"] for row in csv.DictReader(handle)]
        else:
            seeds = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    if not seeds:
        raise ValueError(f"没有在 {path} 中找到 seed")
    for seed in seeds:
        if not 0 <= int(seed, 0) <= 0xFFFFFFFF:
            raise ValueError(f"seed 超出范围: {seed}")
    return seeds


def cpu_times():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    return sum(values), values[3] + values[4]


def available_memory_kib():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    return 0


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merged_results(trial_dir, workers):
    header = None
    rows = []
    for worker_id in range(1, workers + 1):
        path = trial_dir / f"worker-{worker_id}" / "results.csv"
        if not path.exists():
            continue
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if header is None:
                header = reader.fieldnames
            rows.extend(reader)
    if header:
        rows.sort(key=lambda row: (row["timestamp"], int(row["worker"])))
        with (trial_dir / "results.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="按时间或指定 seed 运行 fuznet；等待正在执行的轮次结束后停止。"
    )
    parser.add_argument("--minutes", type=float, default=10, help="运行多少分钟，默认 10")
    parser.add_argument("--workers", type=int, default=1, help="同时运行的 worker 数，默认 1")
    parser.add_argument("--vivado-bin", type=Path,
                        default=Path(os.environ.get("VIVADO_BIN", DEFAULT_VIVADO)),
                        help="Vivado 可执行文件路径，默认使用现有 2024.2")
    parser.add_argument("--seeds-from", type=Path,
                        help="读取旧 results.csv 的 seed，复现相同网表；此时不限制分钟数")
    parser.add_argument("--output", type=Path, help="结果目录，默认在 logs/ 下新建")
    parser.add_argument("--no-keep-all", action="store_true", help="不保存普通轮次的中间目录")
    args = parser.parse_args()

    if args.workers < 1 or args.minutes <= 0:
        parser.error("--workers 必须至少为 1，--minutes 必须大于 0")
    if not args.vivado_bin.is_file() or not os.access(args.vivado_bin, os.X_OK):
        parser.error(f"找不到可执行的 Vivado：{args.vivado_bin}")
    for command in ("fuznet", "yosys", "verilator", "jq"):
        if shutil.which(command) is None:
            parser.error(f"找不到 {command}；请先在项目目录运行 nix develop")

    try:
        seeds = read_seeds(args.seeds_from) if args.seeds_from else None
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))

    trial_dir = args.output or ROOT / "logs" / f"trial-{datetime.now():%Y%m%d-%H%M%S}"
    trial_dir = trial_dir.resolve()
    if trial_dir.exists():
        parser.error(f"结果目录已存在：{trial_dir}；请换一个目录")
    trial_dir.mkdir(parents=True)

    vivado_bin = args.vivado_bin.resolve()
    try:
        version = subprocess.run([str(vivado_bin), "-version"], capture_output=True,
                                 text=True, timeout=30)
        vivado_version = (version.stdout or version.stderr).splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        vivado_version = "无法读取版本信息"
    started = datetime.now().isoformat(timespec="seconds")
    manifest = {
        "started": started,
        "workers": args.workers,
        "minutes": None if seeds is not None else args.minutes,
        "seeds_from": str(args.seeds_from.resolve()) if args.seeds_from else None,
        "vivado_bin": str(vivado_bin),
        "vivado_version": vivado_version,
        "keep_all": not args.no_keep_all,
        "input_sha256": {
            str(path.relative_to(ROOT)): file_sha256(path)
            for path in (ROOT / "config/settings.toml", ROOT / "hardware/xilinx/cells.yaml",
                         ROOT / "hardware/xilinx/cell_sim.v", ROOT / "flows/vivado/impl.tcl")
        },
    }
    (trial_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"结果目录：{trial_dir}", flush=True)
    print(f"worker 数：{args.workers}；Vivado：{vivado_bin}", flush=True)
    if seeds is not None:
        print(f"按旧记录复现 {len(seeds)} 个 seed", flush=True)
    else:
        print(f"运行 {args.minutes:g} 分钟；到时不再启动新一轮，等待当前轮结束", flush=True)

    stop = threading.Event()
    lock = threading.Lock()
    active = {}
    next_seed = 0
    deadline = time.monotonic() + args.minutes * 60
    interrupts = 0

    def handle_signal(_signum, _frame):
        nonlocal interrupts
        interrupts += 1
        stop.set()
        if interrupts == 1:
            print("\n正在等待当前轮结束。再次按 Ctrl+C 将中断当前轮。", flush=True)
        else:
            print("\n正在中断当前轮。", flush=True)
            with lock:
                processes = list(active.values())
            for process in processes:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def worker(worker_id):
        nonlocal next_seed
        worker_dir = trial_dir / f"worker-{worker_id}"
        worker_dir.mkdir()
        log_path = worker_dir / "console.log"
        while not stop.is_set():
            with lock:
                if seeds is None:
                    if time.monotonic() >= deadline:
                        return
                    seed = None
                else:
                    if next_seed >= len(seeds):
                        return
                    seed = seeds[next_seed]
                    next_seed += 1

            env = os.environ.copy()
            env["WORKER_ID"] = str(worker_id)
            env["PERMANENT_LOGS"] = str(worker_dir)
            env["KEEP_OUT_DIR"] = "0" if args.no_keep_all else "1"
            env["VIVADO_BIN"] = str(vivado_bin)
            env.pop("OUT_DIR", None)
            if seed is None:
                env.pop("SEED", None)
            else:
                env["SEED"] = seed

            case_start = time.monotonic()
            with log_path.open("a") as log:
                process = subprocess.Popen(
                    [str(ROOT / "scripts" / "fuzzer.sh")],
                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                with lock:
                    active[worker_id] = process
                returncode = process.wait()
                with lock:
                    active.pop(worker_id, None)

            category = "unknown"
            results_path = worker_dir / "results.csv"
            if results_path.exists():
                with results_path.open(newline="") as handle:
                    rows = list(csv.DictReader(handle))
                if rows:
                    category = rows[-1].get("category", "unknown")
            print(f"worker {worker_id}: {category}, {time.monotonic()-case_start:.1f} 秒"
                  f"（退出码 {returncode}）", flush=True)

    threads = [threading.Thread(target=worker, args=(worker_id,), daemon=False)
               for worker_id in range(1, args.workers + 1)]
    for thread in threads:
        thread.start()

    samples_path = trial_dir / "resource_samples.csv"
    previous_total, previous_idle = cpu_times()
    with samples_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "cpu_busy_percent", "mem_available_gib"])
        while any(thread.is_alive() for thread in threads):
            time.sleep(2)
            total, idle = cpu_times()
            delta_total = total - previous_total
            delta_idle = idle - previous_idle
            busy = 100 * (1 - delta_idle / delta_total) if delta_total else 0
            writer.writerow([datetime.now().isoformat(timespec="seconds"),
                             f"{busy:.1f}", f"{available_memory_kib()/1024/1024:.2f}"])
            handle.flush()
            previous_total, previous_idle = total, idle

    for thread in threads:
        thread.join()
    rows = merged_results(trial_dir, args.workers)
    categories = Counter(row["category"] for row in rows)
    disk = subprocess.run(["du", "-sh", str(trial_dir)], capture_output=True,
                          text=True, check=True).stdout.split()[0]
    summary = [f"结果目录：{trial_dir}", f"记录轮数：{len(rows)}",
               f"目录占用：{disk}", "结果分类："]
    summary.extend(f"  {category}: {count}" for category, count in sorted(categories.items()))
    (trial_dir / "summary.txt").write_text("\n".join(summary) + "\n")
    print("\n".join(summary), flush=True)


if __name__ == "__main__":
    main()
