import argparse
import pandas as pd
import matplotlib.pyplot as plt


def read_eval_csv(path: str):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    if "# ---- stability_summary ----" in lines:
        idx = lines.index("# ---- stability_summary ----")
        main_lines = lines[:idx]
        stab_lines = lines[idx + 1:]
    else:
        main_lines = lines
        stab_lines = []

    from io import StringIO
    df = pd.read_csv(StringIO("\n".join(main_lines)))

    stab = None
    if stab_lines and len(stab_lines) >= 2:
        stab = pd.read_csv(StringIO("\n".join(stab_lines)))

    return df, stab


def ms_to_s(series):
    return series / 1000.0


def savefig(path):
    plt.tight_layout()
    plt.savefig(path, dpi=200)
    plt.close()


def plot_pqs_by_type(df, outdir):
    tmp = df.dropna(subset=["pqs", "signal_type"]).copy()
    tmp["pqs"] = pd.to_numeric(tmp["pqs"], errors="coerce")
    tmp = tmp.dropna(subset=["pqs"])

    types = sorted(tmp["signal_type"].unique())
    data = [tmp[tmp["signal_type"] == t]["pqs"].values for t in types]

    plt.figure()
    # Matplotlib 3.9+: labels renamed to tick_labels
    plt.boxplot(data, tick_labels=types, showfliers=False)
    plt.ylabel("PQS")
    plt.title("Plan Quality Score (PQS) by Anomaly Type")
    plt.xticks(rotation=30, ha="right")
    savefig(f"{outdir}/pqs_by_type.png")


def plot_llm_vs_pqs(df, outdir):
    tmp = df.dropna(subset=["pqs", "llm_ms"]).copy()
    tmp["pqs"] = pd.to_numeric(tmp["pqs"], errors="coerce")
    tmp["llm_ms"] = pd.to_numeric(tmp["llm_ms"], errors="coerce")
    tmp = tmp.dropna(subset=["pqs", "llm_ms"])

    plt.figure()
    plt.scatter(ms_to_s(tmp["llm_ms"]), tmp["pqs"], alpha=0.6)
    plt.xlabel("LLM planning time (s)")
    plt.ylabel("PQS")
    plt.title("LLM Latency vs Quality (per Case)")
    savefig(f"{outdir}/llm_vs_pqs.png")


def plot_queue_vs_pqs(df, outdir):
    # Queue delay ≈ mttr_ms - llm_ms (if mttr_ms includes queueing)
    tmp = df.dropna(subset=["pqs", "mttr_ms", "llm_ms"]).copy()
    tmp["pqs"] = pd.to_numeric(tmp["pqs"], errors="coerce")
    tmp["mttr_ms"] = pd.to_numeric(tmp["mttr_ms"], errors="coerce")
    tmp["llm_ms"] = pd.to_numeric(tmp["llm_ms"], errors="coerce")
    tmp = tmp.dropna(subset=["pqs", "mttr_ms", "llm_ms"])

    tmp["queue_ms"] = tmp["mttr_ms"] - tmp["llm_ms"]
    tmp = tmp[tmp["queue_ms"] >= 0]

    plt.figure()
    plt.scatter(ms_to_s(tmp["queue_ms"]), tmp["pqs"], alpha=0.6)
    plt.xlabel("Queue delay (s) ≈ (mttr_ms - llm_ms)")
    plt.ylabel("PQS")
    plt.title("Queue Delay vs Quality (per Case)")
    savefig(f"{outdir}/queue_vs_pqs.png")


def plot_score_breakdown_by_type(df, outdir):
    cols = ["coverage_fit", "safety_disruption", "completeness", "actionability", "cacao_validity"]
    tmp = df.dropna(subset=["signal_type"]).copy()
    for c in cols:
        tmp[c] = pd.to_numeric(tmp[c], errors="coerce")

    grp = tmp.groupby("signal_type")[cols].mean().fillna(0)

    plt.figure()
    bottom = None
    x = range(len(grp.index))
    for c in cols:
        vals = grp[c].values
        if bottom is None:
            plt.bar(x, vals, label=c)
            bottom = vals
        else:
            plt.bar(x, vals, bottom=bottom, label=c)
            bottom = bottom + vals

    plt.xticks(list(x), list(grp.index), rotation=30, ha="right")
    plt.ylabel("Mean sub-score")
    plt.title("Mean PQS Component Breakdown by Type")
    plt.legend()
    savefig(f"{outdir}/pqs_breakdown_by_type.png")


def plot_stability_vs_quality(stab, outdir):
    if stab is None or stab.empty:
        return

    tmp = stab.copy()
    tmp["stability_jaccard_mean"] = pd.to_numeric(tmp["stability_jaccard_mean"], errors="coerce")
    tmp["avg_pqs"] = pd.to_numeric(tmp["avg_pqs"], errors="coerce")
    tmp = tmp.dropna(subset=["stability_jaccard_mean", "avg_pqs"])

    plt.figure()
    plt.scatter(tmp["avg_pqs"], tmp["stability_jaccard_mean"], alpha=0.7)
    plt.xlabel("Average PQS (per anomaly group)")
    plt.ylabel("Stability (mean Jaccard over action sets)")
    plt.title("Stability vs Quality (per anomaly group)")
    savefig(f"{outdir}/stability_vs_quality.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="poc_eval_results.csv")
    ap.add_argument("--outdir", default="plots", help="output directory")
    args = ap.parse_args()

    import os
    os.makedirs(args.outdir, exist_ok=True)

    df, stab = read_eval_csv(args.csv)

    plot_pqs_by_type(df, args.outdir)
    plot_llm_vs_pqs(df, args.outdir)
    plot_queue_vs_pqs(df, args.outdir)
    plot_score_breakdown_by_type(df, args.outdir)
    plot_stability_vs_quality(stab, args.outdir)

    print(f"[i] saved plots to {args.outdir}/")


if __name__ == "__main__":
    main()