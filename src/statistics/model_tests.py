"""Statistics (H2-H4): are the classifiers' results real, and do they differ?

Usage:
    python -m src.statistics.model_tests

Run after src.data_mining.train (it reuses the tuned settings from reports/data_mining/model_comparison.csv).
All results are per ship (the class with the highest average probability over the ship's windows), except the
leakage test, where a row-wise split has no clean per-ship meaning.

    H2  permutation test: ship types are shuffled between ships, the model is retrained, and its test macro-F1 is
        compared with the real one
    H3  McNemar test on the test ships, and the Nadeau-Bengio corrected t-test over repeated ship-wise
        cross-validation
    H4  leakage: row-wise vs ship-wise split, repeated, paired comparison and bootstrap CI of the difference
    CI  95% bootstrap intervals for macro-F1 and accuracy, resampling test ships

Writes reports/statistics/model_*.csv and leakage_*.csv.
"""
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.base import clone
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold, train_test_split
from statsmodels.stats.contingency_tables import mcnemar

from ..common.config import REPORTS_DIR
from ..data_mining.features import CLASSES, FEATURES
from ..data_mining.train import MODELS, SEED, load, per_ship, ship_split

OUT_DIR = REPORTS_DIR / "statistics"
COMPARED = ["Logistic regression", "Decision tree", "Random forest"]
PERMUTATIONS = 100
PERMUTATION_TREES = 100     # smaller forest for the 100 refits; the real score uses the same setting
CV_REPEATS, CV_FOLDS = 5, 5
LEAKAGE_REPEATS = 10
BOOTSTRAP = 2000


def tuned_models():
    """The three models with the parameters chosen in src.data_mining.train."""
    chosen = pd.read_csv(REPORTS_DIR / "data_mining" / "model_comparison.csv").set_index("model").best_parameters
    models = {}
    for name in COMPARED:
        pipe = clone(MODELS[name][0])
        step = pipe.steps[-1][0]
        for pair in str(chosen[name]).split(", "):
            key, value = pair.split("=")
            number = float(value)
            pipe.set_params(**{f"{step}__{key}": int(number) if number.is_integer() else number})
        models[name] = pipe
    return models


def ship_scores(model, train, test):
    model.fit(train[FEATURES], train.ship_type)
    truth, pred, _ = per_ship(test, model.predict_proba(test[FEATURES]), model.classes_)
    return truth, pred


def permutation_test(models, train, test):
    rng = np.random.default_rng(SEED)
    ships = pd.concat([train, test]).groupby("mmsi").ship_type.first()
    rows = []
    for name, model in models.items():
        if name == "Random forest":
            model = clone(model).set_params(randomforestclassifier__n_estimators=PERMUTATION_TREES)
        truth, pred = ship_scores(model, train, test)
        real = f1_score(truth, pred, average="macro")
        null = []
        for _ in range(PERMUTATIONS):
            shuffled = pd.Series(rng.permutation(ships.values), index=ships.index)
            a, b = train.assign(ship_type=train.mmsi.map(shuffled)), test.assign(ship_type=test.mmsi.map(shuffled))
            t, p = ship_scores(clone(model), a, b)
            null.append(f1_score(t, p, average="macro"))
        null = np.array(null)
        rows.append({"model": name, "macro_f1": real, "null_mean": null.mean(), "null_max": null.max(),
                     "permutations": PERMUTATIONS, "p": (1 + (null >= real).sum()) / (1 + PERMUTATIONS)})
        print(f"permutation {name}: {real:.3f} vs null max {null.max():.3f}")
    return pd.DataFrame(rows)


def bootstrap_ci(predictions):
    rng = np.random.default_rng(SEED)
    rows = []
    for name, (truth, pred) in predictions.items():
        truth, pred = truth.to_numpy(), pred.to_numpy()
        f1s, accs = [], []
        for _ in range(BOOTSTRAP):
            i = rng.integers(0, len(truth), len(truth))
            f1s.append(f1_score(truth[i], pred[i], average="macro"))
            accs.append(accuracy_score(truth[i], pred[i]))
        rows.append({"model": name, "macro_f1": f1_score(truth, pred, average="macro"),
                     "macro_f1_ci_low": np.percentile(f1s, 2.5), "macro_f1_ci_high": np.percentile(f1s, 97.5),
                     "accuracy": accuracy_score(truth, pred),
                     "accuracy_ci_low": np.percentile(accs, 2.5), "accuracy_ci_high": np.percentile(accs, 97.5)})
    return pd.DataFrame(rows)


def mcnemar_tests(predictions):
    rows = []
    best = "Random forest"
    truth, best_pred = predictions[best]
    for name in COMPARED:
        if name == best:
            continue
        other = predictions[name][1]
        a, b = (best_pred == truth).to_numpy(), (other == truth).to_numpy()
        table = [[(a & b).sum(), (a & ~b).sum()], [(~a & b).sum(), (~a & ~b).sum()]]
        result = mcnemar(table, exact=True)
        rows.append({"model_a": best, "model_b": name, "both_right": table[0][0], "only_a_right": table[0][1],
                     "only_b_right": table[1][0], "both_wrong": table[1][1], "p": result.pvalue})
    return pd.DataFrame(rows)


def corrected_t_test(models, data):
    """Nadeau-Bengio: the folds overlap, so the variance of the differences is inflated by n_test / n_train."""
    scores = []
    for repeat in range(CV_REPEATS):
        folds = StratifiedGroupKFold(n_splits=CV_FOLDS, shuffle=True, random_state=repeat)
        for fold, (tr, te) in enumerate(folds.split(data[FEATURES], data.ship_type, groups=data.mmsi)):
            train, test = data.iloc[tr], data.iloc[te]
            for name, model in models.items():
                truth, pred = ship_scores(clone(model), train, test)
                scores.append({"repeat": repeat, "fold": fold, "model": name, "test_ships": test.mmsi.nunique(),
                               "train_ships": train.mmsi.nunique(),
                               "macro_f1": f1_score(truth, pred, average="macro")})
        print(f"cross-validation repeat {repeat + 1}/{CV_REPEATS} done")
    scores = pd.DataFrame(scores)
    wide = scores.pivot_table(index=["repeat", "fold"], columns="model", values="macro_f1")
    ratio = (scores.test_ships / scores.train_ships).mean()
    rows = []
    for name in COMPARED:
        if name == "Random forest":
            continue
        d = wide["Random forest"] - wide[name]
        k = len(d)
        t = d.mean() / np.sqrt((1 / k + ratio) * d.var(ddof=1))
        rows.append({"model_a": "Random forest", "model_b": name, "folds": k, "mean_difference": d.mean(),
                     "sd_difference": d.std(ddof=1), "t": t, "df": k - 1, "p": 2 * stats.t.sf(abs(t), k - 1)})
    return scores, pd.DataFrame(rows)


def leakage_test(model, data):
    rows = []
    for seed in range(LEAKAGE_REPEATS):
        folds = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed)
        tr, te = next(folds.split(data[FEATURES], data.ship_type, groups=data.mmsi))
        splits = {"by_ship": (data.iloc[tr], data.iloc[te]),
                  "by_row": train_test_split(data, test_size=0.2, stratify=data.ship_type, random_state=seed)}
        row = {"repeat": seed}
        for split, (a, b) in splits.items():
            m = clone(model).fit(a[FEATURES], a.ship_type)
            row[split] = f1_score(b.ship_type, m.predict(b[FEATURES]), average="macro")
        rows.append(row)
    detail = pd.DataFrame(rows)
    d = (detail.by_row - detail.by_ship).to_numpy()
    rng = np.random.default_rng(SEED)
    boot = [rng.choice(d, len(d)).mean() for _ in range(BOOTSTRAP)]
    summary = pd.DataFrame([{
        "repeats": len(d), "by_ship_mean": detail.by_ship.mean(), "by_row_mean": detail.by_row.mean(),
        "mean_difference": d.mean(), "difference_ci_low": np.percentile(boot, 2.5),
        "difference_ci_high": np.percentile(boot, 97.5),
        "paired_t_p": stats.ttest_rel(detail.by_row, detail.by_ship).pvalue,
        "wilcoxon_p": stats.wilcoxon(detail.by_row, detail.by_ship).pvalue,
    }])
    return detail, summary


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load()
    data = data[data.ship_type.isin(CLASSES)].reset_index(drop=True)
    train, test = ship_split(data)
    models = tuned_models()

    predictions = {name: ship_scores(clone(m), train, test) for name, m in models.items()}
    ci = bootstrap_ci(predictions)
    ci.to_csv(OUT_DIR / "model_bootstrap_ci.csv", index=False)
    mcn = mcnemar_tests(predictions)
    mcn.to_csv(OUT_DIR / "model_mcnemar.csv", index=False)

    perm = permutation_test(models, train, test)
    perm.to_csv(OUT_DIR / "model_permutation.csv", index=False)

    cv_scores, cv_tests = corrected_t_test(models, data)
    cv_scores.to_csv(OUT_DIR / "model_repeated_cv_scores.csv", index=False)
    cv_tests.to_csv(OUT_DIR / "model_corrected_t_test.csv", index=False)

    leak_detail, leak_summary = leakage_test(models["Random forest"], data)
    leak_detail.to_csv(OUT_DIR / "leakage_repeats.csv", index=False)
    leak_summary.to_csv(OUT_DIR / "leakage_test.csv", index=False)

    pd.set_option("display.width", 200)
    for table in (ci, mcn, perm, cv_tests, leak_summary):
        print(table.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
