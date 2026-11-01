"""Step 4 (Data Mining): predict ship type from movement, evaluate on unseen ships, and label the unknown ships.

Usage:
    python -m src.data_mining.train

Run after src.data_mining.features. Only moving windows are used (stationary ones are left out).

1. Split by ship: about 20% of the ships (all their windows) are held out as the test set. A ship never has
   windows on both sides, otherwise the model could recognise the ship instead of learning the ship type.
2. On the training ships, each model is tuned and scored with 5-fold cross-validation grouped by ship.
3. Each model is fitted on all training ships and scored once on the test ships: per window, and per ship
   (the class with the highest average probability over the ship's windows).
4. Leakage check: the best model is also scored with an ordinary row-level random split, to show how much
   that would overstate the result.
5. The best model (chosen by cross-validation, not by the test set) is refitted on all labelled ships and
   predicts the type of the ships whose AIS type is unknown.

Writes reports/data_mining/*.csv, reports/data_mining/figures/*.png and DATA_DIR/analysis/predicted_types.parquet.
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, classification_report, confusion_matrix,
                             f1_score)
from sklearn.model_selection import GridSearchCV, StratifiedGroupKFold, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

from ..common.config import DATA_DIR, REPORTS_DIR
from .features import CLASSES, FEATURES

SEED = 42
OUT_DIR = REPORTS_DIR / "data_mining"
FIG_DIR = OUT_DIR / "figures"
LEAKAGE_REPEATS = 5
CONFIDENT_PROBABILITY = 0.6

# name -> (pipeline, parameters to tune). Missing values (e.g. no heading in a window) are filled inside the
# pipeline, so the fill values are learned from the training folds only.
MODELS = {
    "Dummy (most frequent)": (make_pipeline(SimpleImputer(), DummyClassifier(strategy="most_frequent")), {}),
    "Dummy (stratified)": (make_pipeline(SimpleImputer(), DummyClassifier(strategy="stratified",
                                                                          random_state=SEED)), {}),
    "Logistic regression": (
        make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                      LogisticRegression(class_weight="balanced", max_iter=3000)),
        {"logisticregression__C": [0.1, 1, 10]}),
    "Decision tree": (
        make_pipeline(SimpleImputer(strategy="median"),
                      DecisionTreeClassifier(class_weight="balanced", random_state=SEED)),
        {"decisiontreeclassifier__max_depth": [4, 6, 8, 12]}),
    "Random forest": (
        make_pipeline(SimpleImputer(strategy="median"),
                      RandomForestClassifier(n_estimators=300, class_weight="balanced", n_jobs=-1,
                                             random_state=SEED)),
        {"randomforestclassifier__min_samples_leaf": [1, 5, 20]}),
}


def load():
    w = pd.read_parquet(DATA_DIR / "analysis" / "windows.parquet")
    return w[~w.stationary].reset_index(drop=True)


def ship_split(data):
    """Hold out about 20% of the ships, keeping the class shares."""
    folds = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=SEED)
    train_idx, test_idx = next(folds.split(data[FEATURES], data.ship_type, groups=data.mmsi))
    return data.iloc[train_idx], data.iloc[test_idx]


def per_ship(windows, proba, classes):
    """One prediction per ship: the class with the highest average probability over its windows."""
    p = pd.DataFrame(proba, columns=classes, index=windows.index)
    p["mmsi"] = windows.mmsi.values
    mean = p.groupby("mmsi")[list(classes)].mean()
    truth = windows.groupby("mmsi").ship_type.first().loc[mean.index]
    return truth, mean.idxmax(axis=1), mean.max(axis=1)


def scores(y_true, y_pred):
    return {"macro_f1": f1_score(y_true, y_pred, average="macro"),
            "balanced_accuracy": balanced_accuracy_score(y_true, y_pred),
            "accuracy": accuracy_score(y_true, y_pred)}


def compare_models(train, test):
    cv = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=SEED)
    rows, fitted = [], {}
    for name, (pipe, grid) in MODELS.items():
        search = GridSearchCV(pipe, grid, scoring="f1_macro", cv=cv, n_jobs=-1)
        search.fit(train[FEATURES], train.ship_type, groups=train.mmsi)
        model = search.best_estimator_
        fitted[name] = (model, search.best_params_)
        best = search.cv_results_
        i = search.best_index_
        window = scores(test.ship_type, model.predict(test[FEATURES]))
        ship_true, ship_pred, _ = per_ship(test, model.predict_proba(test[FEATURES]), model.classes_)
        ship = scores(ship_true, ship_pred)
        rows.append({
            "model": name,
            "best_parameters": ", ".join(f"{k.split('__')[1]}={v}" for k, v in search.best_params_.items()),
            "cv_macro_f1": best["mean_test_score"][i], "cv_macro_f1_std": best["std_test_score"][i],
            **{f"test_window_{k}": v for k, v in window.items()},
            **{f"test_ship_{k}": v for k, v in ship.items()},
        })
        print(f"{name}: CV macro-F1 {best['mean_test_score'][i]:.3f}, test (ships) {ship['macro_f1']:.3f}")
    return pd.DataFrame(rows).round(3), fitted


def leakage_check(data, model):
    """Same model, ship-wise split vs ordinary row-wise split, repeated with different seeds."""
    rows = []
    for seed in range(LEAKAGE_REPEATS):
        folds = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed)
        tr, te = next(folds.split(data[FEATURES], data.ship_type, groups=data.mmsi))
        splits = {
            "By ship (correct)": (data.iloc[tr], data.iloc[te]),
            "By row (leaky)": train_test_split(data, test_size=0.2, stratify=data.ship_type, random_state=seed),
        }
        for split, (a, b) in splits.items():
            m = model.fit(a[FEATURES], a.ship_type)
            rows.append({"repeat": seed, "split": split,
                         "macro_f1": f1_score(b.ship_type, m.predict(b[FEATURES]), average="macro")})
    detail = pd.DataFrame(rows)
    summary = detail.groupby("split").macro_f1.agg(["mean", "std", "min", "max"]).round(3).reset_index()
    return detail.round(3), summary


def plot_confusion(window_cm, ship_cm, classes, path):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for ax, cm, title in [(axes[0], window_cm, "Per window"), (axes[1], ship_cm, "Per ship")]:
        share = cm / cm.sum(axis=1, keepdims=True)
        ax.imshow(share, cmap="Blues", vmin=0, vmax=1)
        for i in range(len(classes)):
            for j in range(len(classes)):
                ax.text(j, i, f"{share[i, j]:.0%}\n({cm[i, j]})", ha="center", va="center",
                        color="white" if share[i, j] > 0.6 else "black", fontsize=9)
        ax.set_xticks(range(len(classes)), classes)
        ax.set_yticks(range(len(classes)), classes)
        ax.set_xlabel("Predicted type")
        ax.set_ylabel("AIS ship type")
        ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def plot_importance(importance, path):
    top = importance.sort_values("importance_mean").tail(15)
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.barh(top.feature, top.importance_mean, xerr=top.importance_std, color="#4C72B0")
    ax.set_xlabel("Drop in macro-F1 when the feature is shuffled")
    ax.set_title("Permutation importance (test ships)")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    data = load()
    labelled = data[data.ship_type.isin(CLASSES)].reset_index(drop=True)
    unknown = data[data.ship_type == "Unknown"].reset_index(drop=True)

    train, test = ship_split(labelled)
    split = pd.DataFrame({"set": ["train", "test"],
                          "ships": [train.mmsi.nunique(), test.mmsi.nunique()],
                          "windows": [len(train), len(test)]})
    split.to_csv(OUT_DIR / "split.csv", index=False)
    print(split.to_string(index=False))

    comparison, fitted = compare_models(train, test)
    comparison.to_csv(OUT_DIR / "model_comparison.csv", index=False)

    # The best model is chosen on cross-validation, so the test set stays an honest check
    real_models = comparison[~comparison.model.str.startswith("Dummy")]
    best_name = real_models.sort_values("cv_macro_f1").model.iloc[-1]
    best, _ = fitted[best_name]
    print(f"Best model by cross-validation: {best_name}")

    classes = list(best.classes_)
    window_pred = best.predict(test[FEATURES])
    ship_true, ship_pred, _ = per_ship(test, best.predict_proba(test[FEATURES]), classes)
    report = pd.DataFrame(classification_report(ship_true, ship_pred, output_dict=True)).T.round(3)
    report.to_csv(OUT_DIR / "best_model_per_class.csv", index_label="class")
    window_cm = confusion_matrix(test.ship_type, window_pred, labels=classes)
    ship_cm = confusion_matrix(ship_true, ship_pred, labels=classes)
    pd.DataFrame(ship_cm, index=classes, columns=classes).to_csv(OUT_DIR / "confusion_matrix_ships.csv",
                                                                 index_label="ais_type")
    pd.DataFrame(window_cm, index=classes, columns=classes).to_csv(OUT_DIR / "confusion_matrix_windows.csv",
                                                                   index_label="ais_type")
    plot_confusion(window_cm, ship_cm, classes, FIG_DIR / "confusion_matrix.png")

    perm = permutation_importance(best, test[FEATURES], test.ship_type, scoring="f1_macro", n_repeats=10,
                                  random_state=SEED, n_jobs=-1)
    importance = pd.DataFrame({"feature": FEATURES, "importance_mean": perm.importances_mean,
                               "importance_std": perm.importances_std}).sort_values("importance_mean",
                                                                                    ascending=False)
    importance.round(4).to_csv(OUT_DIR / "permutation_importance.csv", index=False)
    plot_importance(importance, FIG_DIR / "permutation_importance.png")

    leak_detail, leak_summary = leakage_check(labelled, best)
    leak_detail.to_csv(OUT_DIR / "leakage_detail.csv", index=False)
    leak_summary.to_csv(OUT_DIR / "leakage_summary.csv", index=False)
    print(leak_summary.to_string(index=False))

    # Label the unknown ships with the best model refitted on every labelled ship
    best.fit(labelled[FEATURES], labelled.ship_type)
    p = pd.DataFrame(best.predict_proba(unknown[FEATURES]), columns=best.classes_)
    p["mmsi"] = unknown.mmsi.values
    mean = p.groupby("mmsi")[classes].mean()
    predicted = pd.DataFrame({
        "mmsi": mean.index, "predicted_type": mean.idxmax(axis=1).values,
        "probability": mean.max(axis=1).round(3).values,
        "windows": p.groupby("mmsi").size().loc[mean.index].values,
    })
    predicted["confident"] = predicted.probability >= CONFIDENT_PROBABILITY
    predicted.to_parquet(DATA_DIR / "analysis" / "predicted_types.parquet", index=False)
    unknown_summary = (predicted.groupby("predicted_type")
                       .agg(ships=("mmsi", "size"), confident_ships=("confident", "sum"),
                            median_probability=("probability", "median"))
                       .reset_index())
    unknown_summary.to_csv(OUT_DIR / "unknown_predictions.csv", index=False)

    pd.set_option("display.width", 200)
    print(comparison.to_string(index=False))
    print(report.to_string())
    print(importance.head(8).round(3).to_string(index=False))
    print(unknown_summary.to_string(index=False))


if __name__ == "__main__":
    main()
