from bot.analysis import align_htf, compute_features, resample, trend_bias


def test_features_have_scores(ohlcv, cfg):
    f = compute_features(ohlcv, resample(ohlcv, "4h"), cfg.strategy, "15m", "4h")
    assert f["long_score"].between(0, 1).all()
    assert f["short_score"].between(0, 1).all()
    assert set(f["regime"].unique()) <= {"trend", "range", "volatile"}
    assert (f["long_score"] > 0).any() and (f["short_score"] > 0).any()


def test_htf_alignment_has_no_lookahead(ohlcv, cfg):
    htf = resample(ohlcv, "4h")
    feat = trend_bias(htf, cfg.strategy)
    aligned = align_htf(ohlcv, feat, "15m", "4h")
    # Kerze 00:00-00:15: die 4h-Kerze 00:00-04:00 ist noch NICHT abgeschlossen
    ts = htf.index[100]
    assert aligned.loc[ts, "htf_bias"] == feat["htf_bias"].iloc[99]
    # Die letzte 15m-Kerze in der 4h-Kerze (03:45-04:00) darf den Wert der gerade geschlossenen Kerze sehen
    last = ts + (htf.index[1] - htf.index[0]) - (ohlcv.index[1] - ohlcv.index[0])
    assert aligned.loc[last, "htf_bias"] == feat["htf_bias"].iloc[100]


def test_features_do_not_change_with_future_data(ohlcv, cfg):
    """Werte bis Kerze N dürfen sich nicht ändern, wenn später weitere Kerzen dazukommen."""
    cut = 4000
    a = compute_features(ohlcv.iloc[:cut], resample(ohlcv.iloc[:cut], "4h"), cfg.strategy, "15m", "4h")
    b = compute_features(ohlcv, resample(ohlcv, "4h"), cfg.strategy, "15m", "4h")
    cols = ["long_score", "short_score", "htf_bias"]
    assert (a[cols].iloc[:-20] == b[cols].iloc[: cut - 20]).all().all()
