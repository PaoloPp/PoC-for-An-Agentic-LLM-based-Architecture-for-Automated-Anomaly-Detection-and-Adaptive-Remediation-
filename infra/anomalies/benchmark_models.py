import argparse
import os
import re
import subprocess
from datetime import datetime, timezone


def slugify_model(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def run(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> None:
    print("[cmd]", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True, env=env)


def capture(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> str:
    print("[cmd]", " ".join(cmd))
    out = subprocess.check_output(cmd, cwd=cwd, env=env, text=True)
    return out.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--anomalies", nargs="+", required=True)
    ap.add_argument("-k", "--runs", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--poll", type=float, default=30.0)
    ap.add_argument("--ingest-url", default="http://localhost:8001/confirmed-anomalies")
    ap.add_argument("--db-url", default=os.getenv("DATABASE_URL", ""))
    ap.add_argument("--compose-file", default=None)
    ap.add_argument("--worker-service", default="playbook_agent_worker")
    ap.add_argument("--agent-service", default="playbook_agent")
    ap.add_argument("--recreate-agent-too", action="store_true",
                    help="Also force-recreate the API container so both use the same model (usually not needed).")
    ap.add_argument("--plots", action="store_true")
    ap.add_argument("--plot-script", default="plot_poc_eval_results.py")
    ap.add_argument("--evaluator", default="evaluate_poc_metrics.py")
    args = ap.parse_args()

    if not args.db_url:
        raise SystemExit("DATABASE_URL must be set (or pass --db-url).")

    base_time = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    compose_cmd = ["docker", "compose"]
    if args.compose_file:
        compose_cmd += ["-f", args.compose_file]

    for m in args.models:
        mslug = slugify_model(m)
        run_tag = f"eval-{mslug}-{base_time}"

        out_csv = f"poc_eval_{mslug}.csv"
        out_plot_dir = f"plots_{mslug}"

        print("\n" + "=" * 80)
        print(f"[i] MODEL={m}")
        print(f"[i] run_tag={run_tag}")
        print(f"[i] output={out_csv}")
        print("=" * 80)

        # IMPORTANT: With docker-compose.yml using ${OLLAMA_CHAT_MODEL:-...},
        # we must pass OLLAMA_CHAT_MODEL in the environment of the compose command.
        env = os.environ.copy()
        env["OLLAMA_CHAT_MODEL"] = m
        env["LLM_MODE"] = env.get("LLM_MODE", "ollama")

        # Recreate worker so env is applied (restart is NOT enough)
        print("[i] Recreating worker with OLLAMA_CHAT_MODEL=", m)
        run(compose_cmd + ["up", "-d", "--no-deps", "--force-recreate", args.worker_service], env=env)

        # Optionally recreate API container too (not needed for DB persistence, but nice for consistency)
        if args.recreate_agent_too:
            print("[i] Recreating API service with OLLAMA_CHAT_MODEL=", m)
            run(compose_cmd + ["up", "-d", "--no-deps", "--force-recreate", args.agent_service], env=env)

        # Sanity check: what model is the container actually using?
        try:
            actual = capture(
                compose_cmd + ["exec", "-T", args.worker_service, "sh", "-lc", "echo -n ${OLLAMA_CHAT_MODEL}"],
                env=env,
            )
            print(f"[i] Worker reports OLLAMA_CHAT_MODEL={actual!r}")
            if actual.strip() != m.strip():
                print("[!] WARNING: Worker model mismatch! Expected:", m, "Got:", actual)
                print("    This usually means docker-compose.yml still hardcodes OLLAMA_CHAT_MODEL instead of ${OLLAMA_CHAT_MODEL:-...}.")
        except Exception as e:
            print("[!] Could not verify worker env var inside container:", repr(e))

        # Run evaluator for this model ONLY (unique run_tag ensures no mixing)
        eval_cmd = [
            "python", args.evaluator,
            "--anomalies", *args.anomalies,
            "-k", str(args.runs),
            "--timeout", str(args.timeout),
            "--poll", str(args.poll),
            "--ingest-url", args.ingest_url,
            "--out", out_csv,
            "--run-tag", run_tag,
            "--db-url", args.db_url,
        ]

        env_eval = os.environ.copy()
        env_eval["DATABASE_URL"] = args.db_url

        print("[i] Running evaluator…")
        subprocess.run(eval_cmd, check=True, env=env_eval)

        if args.plots:
            plot_cmd = ["python", args.plot_script, "--csv", out_csv, "--outdir", out_plot_dir]
            print("[i] Plotting…")
            subprocess.run(plot_cmd, check=True)

        print(f"[i] Done model={m} -> {out_csv} ({out_plot_dir if args.plots else 'no plots'})")


if __name__ == "__main__":
    main()