"""Statistics (H1): do ship types move differently? Tests on one row per ship.

Usage:
    python -m src.statistics.group_tests

Run after src.data_mining.features. Windows of the same ship are not independent, so every test here uses one
observation per ship: the median of each feature over the ship's moving windows.

Writes to reports/statistics/:
    vessel_features.csv     the ship-level table (so the tests can be repeated in SPSS or R)
    descriptives.csv        mean, SD, median, IQR, skewness, kurtosis per class and feature
    assumptions.csv         Shapiro-Wilk per class and Levene (median-centred) per feature
    kruskal_wallis.csv      H, p, Holm-corrected p and epsilon-squared per feature
    pairwise.csv            Dunn post-hoc (Holm within each feature) and Cliff's delta per pair of classes
    spearman.csv, vif.csv   correlation between features and the multicollinearity check
    mnlogit_*.csv           multinomial logistic regression on standardised features, Cargo as reference
    figures/boxplots.png
"""
from itertools import combinations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.outliers_influence import variance_inflation_factor

from ..common.config import DATA_DIR, REPORTS_DIR
from ..data_mining.features import CLASSES, FEATURES

OUT_DIR = REPORTS_DIR / "statistics"
ALPHA = 0.05
MAX_VIF = 10
REFERENCE = "Cargo"
BOXPLOT_FEATURES = ["speed_mean", "speed_cv", "turn_rate_mean", "heading_rate_mean", "stop_ratio", "straightness"]


def vessel_table():
    w = pd.read_parquet(DATA_DIR / "analysis" / "windows.parquet")
    w = w[~w.stationary & w.ship_type.isin(CLASSES)]
    vessels = w.groupby("mmsi").agg(ship_type=("ship_type", "first"), windows=("ship_type", "size"),
                                    **{f: (f, "median") for f in FEATURES})
    return vessels.reset_index()


def descriptives(v):
    rows = []
    for (ship_type, feature), x in v.melt(id_vars="ship_type", value_vars=FEATURES).dropna() \
            .groupby(["ship_type", "variable"]).value:
        q1, q3 = x.quantile([0.25, 0.75])
        rows.append({"feature": feature, "ship_type": ship_type, "n": len(x), "mean": x.mean(), "sd": x.std(),
                     "median": x.median(), "iqr": q3 - q1, "skewness": stats.skew(x), "kurtosis": stats.kurtosis(x)})
    return pd.DataFrame(rows).sort_values(["feature", "ship_type"])


def assumptions(v):
    rows = []
    for f in FEATURES:
        groups = [v.loc[v.ship_type == c, f].dropna() for c in CLASSES]
        row = {"feature": f, "levene_p": stats.levene(*groups, center="median").pvalue}
        for c, g in zip(CLASSES, groups):
            row[f"shapiro_p_{c}"] = stats.shapiro(g).pvalue
        rows.append(row)
    out = pd.DataFrame(rows)
    shapiro_cols = [c for c in out.columns if c.startswith("shapiro")]
    out["normal_in_all_classes"] = (out[shapiro_cols] >= ALPHA).all(axis=1)
    out["equal_variances"] = out.levene_p >= ALPHA
    return out


def dunn(values, labels, a, b):
    """Dunn's z test for one pair, ranks taken over all classes, with the correction for ties."""
    ranks = stats.rankdata(values)
    n = len(values)
    _, counts = np.unique(values, return_counts=True)
    ties = (counts ** 3 - counts).sum() / (12 * (n - 1))
    ra, rb = ranks[labels == a], ranks[labels == b]
    se = np.sqrt((n * (n + 1) / 12 - ties) * (1 / len(ra) + 1 / len(rb)))
    z = (ra.mean() - rb.mean()) / se
    return z, 2 * stats.norm.sf(abs(z))


def cliffs_delta(x, y):
    """P(x > y) - P(x < y), from the Mann-Whitney U of x."""
    u = stats.mannwhitneyu(x, y).statistic
    return 2 * u / (len(x) * len(y)) - 1


def group_tests(v):
    kw_rows, pair_rows = [], []
    for f in FEATURES:
        d = v[["ship_type", f]].dropna()
        groups = [d.loc[d.ship_type == c, f] for c in CLASSES]
        h, p = stats.kruskal(*groups)
        # epsilon-squared for Kruskal-Wallis: H / (n - 1)
        kw_rows.append({"feature": f, "n": len(d), "H": h, "p": p, "epsilon_sq": h / (len(d) - 1)})

        values, labels = d[f].to_numpy(), d.ship_type.to_numpy()
        pairs = []
        for a, b in combinations(CLASSES, 2):
            z, pz = dunn(values, labels, a, b)
            pairs.append({"feature": f, "class_a": a, "class_b": b, "dunn_z": z, "dunn_p": pz,
                          "cliffs_delta": cliffs_delta(d.loc[d.ship_type == a, f], d.loc[d.ship_type == b, f])})
        holm = multipletests([r["dunn_p"] for r in pairs], method="holm")[1]
        for r, ph in zip(pairs, holm):
            r["dunn_p_holm"] = ph
        pair_rows += pairs

    kw = pd.DataFrame(kw_rows)
    kw["p_holm"] = multipletests(kw.p, method="holm")[1]
    kw["significant"] = kw.p_holm < ALPHA
    pairs = pd.DataFrame(pair_rows)
    pairs["significant"] = pairs.dunn_p_holm < ALPHA
    # Common reading of |delta|: < 0.147 negligible, < 0.33 small, < 0.474 medium, otherwise large
    pairs["delta_size"] = pd.cut(pairs.cliffs_delta.abs(), [0, 0.147, 0.33, 0.474, 1.0001],
                                 labels=["negligible", "small", "medium", "large"], right=False)
    return kw.sort_values("epsilon_sq", ascending=False), pairs


def select_by_vif(x):
    """Drop the feature with the highest VIF until every VIF is below MAX_VIF."""
    kept = list(x.columns)
    while True:
        z = sm.add_constant((x[kept] - x[kept].mean()) / x[kept].std())
        vif = pd.Series([variance_inflation_factor(z.values, i + 1) for i in range(len(kept))], index=kept)
        if vif.max() < MAX_VIF:
            return kept, vif
        kept.remove(vif.idxmax())


def mnlogit(v, features):
    d = v[["ship_type"] + features].dropna()
    x = sm.add_constant((d[features] - d[features].mean()) / d[features].std())
    # MNLogit codes classes in sorted order and uses the first as reference: put Cargo first
    order = [REFERENCE] + [c for c in CLASSES if c != REFERENCE]
    y = pd.Categorical(d.ship_type, categories=order).codes
    fit = sm.MNLogit(y, x).fit(method="bfgs", maxiter=5000, disp=False)
    z = stats.norm.ppf(1 - ALPHA / 2)
    rows = []
    # params, bse and pvalues have one column per non-reference class, in the order of `order`
    for j, cls in enumerate(order[1:]):
        for term in x.columns:
            coef, se = fit.params.iloc[:, j][term], fit.bse.iloc[:, j][term]
            rows.append({"class_vs_cargo": cls, "term": term, "coef": coef, "se": se, "odds_ratio": np.exp(coef),
                         "or_ci_low": np.exp(coef - z * se), "or_ci_high": np.exp(coef + z * se),
                         "p": fit.pvalues.iloc[:, j][term]})
    model = pd.DataFrame([{
        "ships": len(d), "features": len(features), "converged": fit.mle_retvals["converged"],
        "log_likelihood": fit.llf, "null_log_likelihood": fit.llnull,
        "lr_chi2": fit.llr, "lr_df": fit.df_model, "lr_p": fit.llr_pvalue, "mcfadden_r2": fit.prsquared,
    }])
    return pd.DataFrame(rows), model


def boxplots(v, path):
    fig, axes = plt.subplots(2, 3, figsize=(13, 7))
    for ax, f in zip(axes.flat, BOXPLOT_FEATURES):
        data = [v.loc[v.ship_type == c, f].dropna() for c in CLASSES]
        ax.boxplot(data, showfliers=False)
        ax.set_xticks(range(1, len(CLASSES) + 1), CLASSES)
        ax.set_title(f)
    fig.suptitle("Ship-level medians by AIS ship type (outliers hidden)")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main():
    (OUT_DIR / "figures").mkdir(parents=True, exist_ok=True)
    v = vessel_table()
    v.to_csv(OUT_DIR / "vessel_features.csv", index=False)
    print(v.ship_type.value_counts().to_string())

    descriptives(v).round(4).to_csv(OUT_DIR / "descriptives.csv", index=False)
    checks = assumptions(v)
    checks.to_csv(OUT_DIR / "assumptions.csv", index=False)
    print(f"Features normal in all classes: {checks.normal_in_all_classes.sum()} of {len(checks)}; "
          f"equal variances: {checks.equal_variances.sum()} of {len(checks)}")

    kw, pairs = group_tests(v)
    kw.to_csv(OUT_DIR / "kruskal_wallis.csv", index=False)
    pairs.to_csv(OUT_DIR / "pairwise.csv", index=False)

    v[FEATURES].corr(method="spearman").round(3).to_csv(OUT_DIR / "spearman.csv")
    kept, vif = select_by_vif(v[FEATURES].dropna())
    vif_all = pd.DataFrame({"feature": FEATURES, "kept_for_regression": [f in kept for f in FEATURES]})
    vif_all = vif_all.merge(vif.rename("vif_after_selection"), left_on="feature", right_index=True, how="left")
    vif_all.to_csv(OUT_DIR / "vif.csv", index=False)

    coefs, model = mnlogit(v, kept)
    coefs.to_csv(OUT_DIR / "mnlogit_coefficients.csv", index=False)
    model.to_csv(OUT_DIR / "mnlogit_model.csv", index=False)
    boxplots(v, OUT_DIR / "figures" / "boxplots.png")

    pd.set_option("display.width", 200)
    print(kw.round(4).to_string(index=False))
    print(pairs.groupby(["class_a", "class_b"]).agg(significant=("significant", "sum"),
                                                    large=("delta_size", lambda s: (s == "large").sum())))
    print("Kept for regression:", kept)
    print(model.round(4).to_string(index=False))
    print(coefs[coefs.p < ALPHA].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
