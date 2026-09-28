#!/bin/sh
# Полный headless-регресс: все tests/test_*.py кроме требующих дисплея.
# Qt-рендер тесты (qt_*, test_v195_qt, test_legacy_live) — только на
# Windows (в песочнице нет libEGL/PySide6).
# Использование: sh scripts/run_regression.sh [python]
PY="${1:-python3}"
cd "$(dirname "$0")/.." || exit 1
total_ok=0
total_fail=0
files=0
fail_files=0
for f in tests/test_*.py; do
    b=$(basename "$f")
    case "$b" in
        test_qt_*|test_v195_qt.py|test_legacy_live*) continue ;;
    esac
    out=$("$PY" "$f" 2>&1)
    rc=$?
    tail_line=$(printf '%s\n' "$out" | grep -E "Итого|ИТОГО|OK / |OK, .* FAIL" | tail -1)
    echo "$b :: rc=$rc :: $tail_line"
    files=$((files + 1))
    if [ "$rc" -ne 0 ]; then
        fail_files=$((fail_files + 1))
        printf '%s\n' "$out" | tail -20
    else
        # v3.6.5: сводки вида «PASS=40 FAIL=0» греп «[0-9]+ FAIL» читал как
        # «40 FAIL» — в общем итоге появлялись фантомные FAIL (87 = 40+16+31).
        # Нормализуем PASS=/FAIL= в «N OK»/«N FAIL» перед подсчётом.
        norm=$(printf '%s\n' "$out" | sed 's/PASS=\([0-9]*\)/\1 OK/g; s/FAIL=\([0-9]*\)/\1 FAIL/g')
        ok=$(printf '%s\n' "$norm" | grep -oE "[0-9]+ OK" | tail -1 | grep -oE "^[0-9]+")
        failn=$(printf '%s\n' "$norm" | grep -oE "[0-9]+ FAIL" | tail -1 | grep -oE "^[0-9]+")
        total_ok=$((total_ok + ${ok:-0}))
        total_fail=$((total_fail + ${failn:-0}))
    fi
done
echo "=== files=$files (fail_files=$fail_files)  checks: $total_ok OK / $total_fail FAIL ==="
[ "$total_fail" -eq 0 ] && [ "$fail_files" -eq 0 ] || exit 1
