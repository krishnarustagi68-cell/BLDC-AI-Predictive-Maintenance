"""
============================================================
 BLDC Motor Health Monitoring — ML Model Training
 File: python/train_models.py
============================================================
 Trains and compares three ML models:
   1. K-Means Clustering       (Unsupervised)
   2. Logistic Regression      (Supervised, replaces Linear for classification)
   3. Decision Tree Classifier (Supervised)

 Outputs:
   - Accuracy comparison table
   - Confusion matrices
   - Saved models (.pkl) for GUI real-time prediction
   - Accuracy chart (matplotlib)

 Usage:
   python train_models.py
   python train_models.py --data ../dataset/motor_data.csv
============================================================
"""

import os
import argparse
import pickle
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.cluster import KMeans
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier, plot_tree
from sklearn.metrics import (
    accuracy_score, confusion_matrix, classification_report,
    ConfusionMatrixDisplay
)

# ─── Configuration ─────────────────────────────────────────────
DATA_FILE    = "../dataset/motor_data.csv"
MODELS_DIR   = "../models"
FEATURE_COLS = ["Temperature", "VibrationX", "VibrationY",
                "VibrationZ", "Current", "RPM"]
LABEL_COL    = "Condition"
TEST_SIZE    = 0.25
RANDOM_STATE = 42
N_CLUSTERS   = 4  # One per fault type


# ─── Data Loading & Preprocessing ─────────────────────────────

def load_data(filepath: str) -> tuple:
    """Load CSV, validate, and return features + labels."""
    print(f"\n  📂 Loading dataset: {filepath}")
    df = pd.read_csv(filepath)

    required = FEATURE_COLS + [LABEL_COL]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns in CSV: {missing}")

    # Drop rows with NaN values
    before = len(df)
    df = df.dropna(subset=required)
    after = len(df)
    if before != after:
        print(f"  ⚠️  Dropped {before - after} rows with missing values.")

    print(f"  ✅ Loaded {len(df)} samples.")
    print(f"  📊 Class distribution:")
    for label, count in df[LABEL_COL].value_counts().items():
        bar = "█" * (count // 5)
        print(f"       {label:<15} {count:>4}  {bar}")

    X = df[FEATURE_COLS].values
    y = df[LABEL_COL].values
    return X, y, df


# ─── Model Training Functions ──────────────────────────────────

def train_kmeans(X_train, X_test, y_train, y_test, le: LabelEncoder, scaler: StandardScaler):
    """
    K-Means Clustering (Unsupervised).
    Since K-Means doesn't use labels during training, we map cluster IDs
    to condition labels by majority vote after fitting.
    """
    print("\n  🔵 Training K-Means Clustering...")

    X_train_sc = scaler.transform(X_train)
    X_test_sc  = scaler.transform(X_test)

    km = KMeans(n_clusters=N_CLUSTERS, random_state=RANDOM_STATE, n_init=20)
    km.fit(X_train_sc)

    # Map cluster IDs → condition labels via majority vote
    train_clusters = km.predict(X_train_sc)
    cluster_to_label = {}
    for c in range(N_CLUSTERS):
        mask = train_clusters == c
        if mask.sum() > 0:
            labels_in_cluster = y_train[mask]
            cluster_to_label[c] = pd.Series(labels_in_cluster).mode()[0]
        else:
            cluster_to_label[c] = "Normal"

    # Predict on test set
    test_clusters = km.predict(X_test_sc)
    y_pred = np.array([cluster_to_label[c] for c in test_clusters])

    acc = accuracy_score(y_test, y_pred)
    cm  = confusion_matrix(y_test, y_pred, labels=le.classes_)
    print(f"       Accuracy: {acc*100:.2f}%")
    return km, y_pred, acc, cm, cluster_to_label


def train_logistic_regression(X_train, X_test, y_train, y_test, le, scaler):
    """
    Logistic Regression (multi-class classification).
    Better suited than Linear Regression for class prediction.
    """
    print("\n  🟡 Training Logistic Regression...")

    X_train_sc = scaler.transform(X_train)
    X_test_sc  = scaler.transform(X_test)

    lr = LogisticRegression(max_iter=1000, random_state=RANDOM_STATE,
                            solver="lbfgs")
    lr.fit(X_train_sc, y_train)

    y_pred = lr.predict(X_test_sc)
    acc    = accuracy_score(y_test, y_pred)
    cm     = confusion_matrix(y_test, y_pred, labels=le.classes_)
    print(f"       Accuracy: {acc*100:.2f}%")
    return lr, y_pred, acc, cm


def train_decision_tree(X_train, X_test, y_train, y_test, le):
    """Decision Tree Classifier — interpretable, handles non-linear boundaries."""
    print("\n  🟢 Training Decision Tree Classifier...")

    dt = DecisionTreeClassifier(max_depth=8, random_state=RANDOM_STATE,
                                min_samples_leaf=5)
    dt.fit(X_train, y_train)

    y_pred = dt.predict(X_test)
    acc    = accuracy_score(y_test, y_pred)
    cm     = confusion_matrix(y_test, y_pred, labels=le.classes_)
    print(f"       Accuracy: {acc*100:.2f}%")
    return dt, y_pred, acc, cm


# ─── Visualization ─────────────────────────────────────────────

def plot_results(results: dict, y_test, le: LabelEncoder):
    """
    Create a 2×3 figure showing:
      - Model accuracy bar chart
      - Confusion matrix for each model
      - Feature importance (Decision Tree)
    """
    labels  = le.classes_
    models  = list(results.keys())
    accs    = [results[m]["accuracy"] for m in models]
    colors  = ["#3B82F6", "#F59E0B", "#10B981"]

    fig = plt.figure(figsize=(18, 12), facecolor="#0F172A")
    fig.suptitle("BLDC Motor Health Monitoring — ML Model Results",
                 color="white", fontsize=16, fontweight="bold", y=0.98)

    gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.35)

    # ── Accuracy Bar Chart ──
    ax0 = fig.add_subplot(gs[0, :])
    bars = ax0.bar(models, [a * 100 for a in accs], color=colors,
                   width=0.4, edgecolor="white", linewidth=0.5)
    ax0.set_facecolor("#1E293B")
    ax0.set_ylim(0, 110)
    ax0.set_ylabel("Accuracy (%)", color="white")
    ax0.set_title("Model Accuracy Comparison", color="white", fontsize=12)
    ax0.tick_params(colors="white")
    for bar, acc in zip(bars, accs):
        ax0.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                 f"{acc*100:.1f}%", ha="center", va="bottom",
                 color="white", fontweight="bold", fontsize=11)
    ax0.spines[:].set_color("#334155")

    # ── Confusion Matrices ──
    for i, (model_name, color) in enumerate(zip(models, colors)):
        ax = fig.add_subplot(gs[1, i])
        cm = results[model_name]["cm"]
        disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=labels)
        disp.plot(ax=ax, colorbar=False, cmap="Blues", xticks_rotation=30)
        ax.set_facecolor("#1E293B")
        ax.set_title(f"{model_name}\n({results[model_name]['accuracy']*100:.1f}%)",
                     color=color, fontsize=10, fontweight="bold")
        ax.tick_params(colors="white", labelsize=7)
        ax.set_xlabel("Predicted", color="white", fontsize=8)
        ax.set_ylabel("Actual", color="white", fontsize=8)
        for txt in ax.texts:
            txt.set_color("white" if int(txt.get_text() or 0) > 5 else "#64748B")

    plt.savefig("../models/model_comparison.png", dpi=150,
                bbox_inches="tight", facecolor="#0F172A")
    print("\n  📊 Chart saved → models/model_comparison.png")
    # plt.show()


# ─── Save Models ───────────────────────────────────────────────

def save_models(km, km_map, lr, dt, scaler, le):
    """Pickle all models + preprocessing objects for GUI use."""
    os.makedirs(MODELS_DIR, exist_ok=True)

    def save(obj, name):
        path = os.path.join(MODELS_DIR, name)
        with open(path, "wb") as f:
            pickle.dump(obj, f)
        print(f"  💾 Saved: {path}")

    save(km,      "kmeans.pkl")
    save(km_map,  "kmeans_label_map.pkl")
    save(lr,      "logistic_regression.pkl")
    save(dt,      "decision_tree.pkl")
    save(scaler,  "scaler.pkl")
    save(le,      "label_encoder.pkl")
    save(FEATURE_COLS, "feature_list.pkl")


# ─── Main ─────────────────────────────────────────────────────

def main(data_file: str):
    X, y, df = load_data(data_file)

    # Encode string labels → integers
    le = LabelEncoder()
    y_enc = le.fit_transform(y)

    # Train/test split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y
    )
    X_train_enc, X_test_enc, yt_train, yt_test = train_test_split(
        X, y_enc, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y_enc
    )

    # Fit scaler on training data only
    scaler = StandardScaler()
    scaler.fit(X_train)

    # ── Train models ──
    km, y_pred_km, acc_km, cm_km, km_map = train_kmeans(
        X_train, X_test, y_train, y_test, le, scaler)

    lr, y_pred_lr, acc_lr, cm_lr = train_logistic_regression(
        X_train, X_test, y_train, y_test, le, scaler)

    dt, y_pred_dt, acc_dt, cm_dt = train_decision_tree(
        X_train, X_test, y_train, y_test, le)

    # ── Results summary ──
    results = {
        "K-Means":             {"accuracy": acc_km, "cm": cm_km, "pred": y_pred_km},
        "Logistic Regression": {"accuracy": acc_lr, "cm": cm_lr, "pred": y_pred_lr},
        "Decision Tree":       {"accuracy": acc_dt, "cm": cm_dt, "pred": y_pred_dt},
    }

    print("\n" + "="*60)
    print("  MODEL COMPARISON TABLE")
    print("="*60)
    print(f"  {'Model':<25} {'Accuracy':>10}")
    print("  " + "-"*35)
    for name, r in results.items():
        print(f"  {name:<25} {r['accuracy']*100:>9.2f}%")

    best = max(results, key=lambda k: results[k]["accuracy"])
    print("  " + "-"*35)
    print(f"  🏆 Best Model: {best}  ({results[best]['accuracy']*100:.2f}%)")
    print("="*60)

    # Detailed report for best model
    print(f"\n  📋 Classification Report — {best}:")
    print(classification_report(y_test, results[best]["pred"], target_names=le.classes_))

    # ── Save models ──
    save_models(km, km_map, lr, dt, scaler, le)

    # ── Plot results ──
    plot_results(results, y_test, le)

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BLDC Motor — ML Training")
    parser.add_argument("--data", type=str, default=DATA_FILE,
                        help="Path to motor_data.csv")
    args = parser.parse_args()
    main(args.data)
