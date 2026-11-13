"""本物の bcsfe を入れた環境で、このアプリが使う機能がそろっているか調べる。

    pip install flask bcsfe
    python smoke_bcsfe.py

足りないものがあると終了コード1で終わる（GitHub Actions の自動更新はこれが通ったときだけ進む）。
ゲームのサーバーには接続しない。
"""
import sys

try:
    import engine
except Exception as e:  # noqa: BLE001
    print("NG: engine.py を読み込めませんでした（bcsfe の入口が変わった可能性があります）:", repr(e))
    sys.exit(1)

problems = list(engine.check_bcsfe_api())

try:
    gv = engine.core.GameVersion.from_string(engine.GAME_DATA_VERSION)
    got = engine.version_to_string(gv)
    print(f"GameVersion.from_string({engine.GAME_DATA_VERSION}) → {got}")
    if got != engine.GAME_DATA_VERSION:
        problems.append(f"GameVersion の文字列化が一致しません: {got}")
except Exception as e:  # noqa: BLE001
    problems.append(f"GameVersion.from_string が使えません: {e!r}")

# テンプレートセーブが読めるか（読めなくても致命的ではないので、注意だけ出す）
try:
    cc = engine.core.CountryCode.from_code("jp")
    raw = engine._TEMPLATE_SAV_FILE.read_bytes()
    template_save = engine.core.SaveFile(engine.core.Data(raw), cc=cc)
    print("template_save.sav: 読み込みOK")
    import save_audit
    template_audit = save_audit.audit_save(
        template_save, known_error_cat_ids=engine.ERROR_CAT_IDS
    )
    print(
        "template_save.sav: 構造監査 "
        f"重大 {template_audit.critical_count} / 注意 {template_audit.warning_count}"
    )
    if not template_audit.ok:
        problems.extend(
            "テンプレートセーブの構造異常: " + finding.code + " @ " + finding.path
            for finding in template_audit.findings
            if finding.severity == "critical"
        )

    # Verify combo fields with the real BCSFE binary serializer, not a fake
    # pickle-based test double. GameVersion is a wrapper, so compare its
    # normalized semantic version rather than coercing the object with int().
    template_version = engine.version_to_string(
        getattr(template_save, "game_version", None)
    )
    if template_version and engine.version_key(template_version) >= engine.version_key("15.0.0"):
        import save_repair

        combo_sentinel = [0, 3, 7, 31, 127]
        template_save.combo_unlocks = list(combo_sentinel)
        template_save.combo_unlocked_10k_ur = True

        def _serialize_real(save):
            return save.to_data().get_bytes()

        def _deserialize_real(payload):
            return engine.core.SaveFile(engine.core.Data(payload), cc=cc)

        binary_roundtrip = _deserialize_real(_serialize_real(template_save))
        if binary_roundtrip.combo_unlocks != combo_sentinel:
            problems.append("BCSFE実バイナリ往復で combo_unlocks が一致しません")
        if binary_roundtrip.combo_unlocked_10k_ur is not True:
            problems.append("BCSFE実バイナリ往復で combo_unlocked_10k_ur が一致しません")

        # Also exercise the actual repair wrapper with the real serializer.
        repaired_save, _, _ = save_repair.repair_unowned_visibility_with_roundtrip_verification(
            template_save,
            _serialize_real,
            _deserialize_real,
            lambda candidate: save_audit.audit_save(
                candidate, known_error_cat_ids=engine.ERROR_CAT_IDS
            ),
        )
        if repaired_save.combo_unlocks != combo_sentinel:
            problems.append("未所持記録整理の実BCSFE往復で combo_unlocks が変化しました")
        if repaired_save.combo_unlocked_10k_ur is not True:
            problems.append("未所持記録整理の実BCSFE往復で combo_unlocked_10k_ur が変化しました")
        print("BCSFE実バイナリ往復: combo_unlocks / combo_unlocked_10k_ur 保持OK")
    else:
        problems.append("現在のBCSFEゲームバージョンでは combo_unlocks の往復検査条件を満たしません")
except Exception as e:  # noqa: BLE001
    print("注意: template_save.sav を読み込めませんでした（初期アカウント作成に影響する可能性）:", repr(e))

if problems:
    print("NG: 足りない・変わった機能があります:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("OK: bcsfe の機能はすべてそろっています")
