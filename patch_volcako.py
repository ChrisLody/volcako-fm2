#!/usr/bin/env python3
"""Patch upstream Luomu/Volcako (master) for Korg Volca FM2.

Keeps the working FM2 operator-mute / Yamaha checksum changes, renames the
visible compatibility setting, and adds mouse-wheel value editing.

Run by the existing GitHub Actions workflow:
    python patch_volcako.py volcako-source

MIT-licensed modifications; original Volcako by Luomu (MIT).
"""
from pathlib import Path
import sys

# Exact upstream anchors. Fail safely rather than silently editing unknown code.
OLD_SERIALISE = '''    op1.to_message(m);
    op2.to_message(m);
    op3.to_message(m);
    op4.to_message(m);
    op5.to_message(m);
    op6.to_message(m);
'''
NEW_SERIALISE = OLD_SERIALISE + '''
    // FM2 compatibility: standard DX7 dump has no operator on/off byte.
    // Mute in outgoing data only; retain stored output level for unmute.
    if (dx7_compat)
    {
        if (!op1.is_on) m[SX_OP1_OUTPUT_LEVEL] = 0;
        if (!op2.is_on) m[SX_OP2_OUTPUT_LEVEL] = 0;
        if (!op3.is_on) m[SX_OP3_OUTPUT_LEVEL] = 0;
        if (!op4.is_on) m[SX_OP4_OUTPUT_LEVEL] = 0;
        if (!op5.is_on) m[SX_OP5_OUTPUT_LEVEL] = 0;
        if (!op6.is_on) m[SX_OP6_OUTPUT_LEVEL] = 0;
    }
'''
OLD_CHECKSUM = '''    return 128 - sum;
'''
NEW_CHECKSUM = '''    // Valid Yamaha SysEx checksums must be seven-bit, including sum == 0.
    return (128 - sum) & 0x7f;
'''

# The original uses Dear ImGui 1.87. All functions here use public API only.
# One accumulated scroll tick changes the displayed value, never beyond bounds.
# Most controls (range > 20): normal = +/-5, Shift = +/-1.
# Small controls (range <= 20): always +/-1.
# Touchpad fractional wheel deltas are accumulated, not rounded up per frame.
WHEEL_FUNCTIONS = r'''// Volcako FM2: wheel handling shared by all numerical controls.
// Positive wheel delta (up) increases value; negative (down) decreases it.
// ImGui calls this once for each widget after the widget has been drawn.
static bool wheel_adjust_int(int& value, int min_value, int max_value)
{
    if (!ImGui::IsItemHovered() || ImGui::IsItemActive())
        return false;

    const float wheel = ImGui::GetIO().MouseWheel;
    // Remember fractional movements from Windows precision touchpads.
    static float remainder = 0.f;
    static ImVec2 last_item_min(-10000.f, -10000.f);
    const ImVec2 item_min = ImGui::GetItemRectMin();
    if (item_min.x != last_item_min.x || item_min.y != last_item_min.y)
    {
        remainder = 0.f;
        last_item_min = item_min;
    }
    if (wheel == 0.f)
        return false;

    remainder += wheel;
    int ticks = static_cast<int>(remainder); // truncate toward zero
    remainder -= static_cast<float>(ticks);
    if (ticks == 0)
        return false;

    const int step = (max_value - min_value <= 20 || ImGui::GetIO().KeyShift) ? 1 : 5;
    const int old_value = value;
    const long long proposed = static_cast<long long>(value) + static_cast<long long>(ticks) * step;
    if (proposed < min_value)
        value = min_value;
    else if (proposed > max_value)
        value = max_value;
    else
        value = static_cast<int>(proposed);
    return value != old_value;
}

static bool wheel_adjust_u8(u8& value, int min_value, int max_value)
{
    int current = static_cast<int>(value);
    const bool changed = wheel_adjust_int(current, min_value, max_value);
    if (changed)
        value = static_cast<u8>(current);
    return changed;
}

static bool wheel_slider_int(const char* label, int& value, int min_value, int max_value, const char* format = "%d")
{
    const bool dragged = ImGui::SliderInt(label, &value, min_value, max_value, format);
    const bool scrolled = wheel_adjust_int(value, min_value, max_value);
    return dragged || scrolled;
}

static bool wheel_slider_u8(const char* label, u8& value, int min_value, int max_value, const char* format)
{
    // For ImGuiDataType_U8, bound pointers must also point to u8 values.
    const u8 lo = static_cast<u8>(min_value);
    const u8 hi = static_cast<u8>(max_value);
    const bool dragged = ImGui::SliderScalar(label, ImGuiDataType_U8, &value, &lo, &hi, format);
    const bool scrolled = wheel_adjust_u8(value, min_value, max_value);
    return dragged || scrolled;
}

static bool wheel_combo_int(const char* label, int& value, const char* const items[], int item_count)
{
    const bool clicked = ImGui::Combo(label, &value, items, item_count);
    const bool scrolled = wheel_adjust_int(value, 0, item_count - 1);
    return clicked || scrolled;
}

// Draw four individually hoverable envelope values, preserving R/L layout.
static bool wheel_drag_int4(const char* label, int values[4], int min_value, int max_value)
{
    bool changed = false;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float full_width = ImGui::CalcItemWidth();
    const float component_width = (full_width - 3.0f * style.ItemInnerSpacing.x) / 4.0f;
    // Keep the original right-click envelope copy/paste behaviour on the row.
    ImGui::BeginGroup();
    ImGui::PushID(label);
    ImGui::PushItemWidth(component_width);
    for (int i = 0; i < 4; ++i)
    {
        if (i > 0)
            ImGui::SameLine(0.f, style.ItemInnerSpacing.x);
        ImGui::PushID(i);
        const bool dragged = ImGui::DragInt("##value", &values[i], 0.4f, min_value, max_value);
        const bool scrolled = wheel_adjust_int(values[i], min_value, max_value);
        changed |= dragged || scrolled;
        ImGui::PopID();
    }
    ImGui::PopItemWidth();
    ImGui::PopID();
    ImGui::SameLine(0.f, style.ItemInnerSpacing.x);
    ImGui::TextUnformatted(label);
    ImGui::EndGroup();
    return changed;
}

'''


def replace_one(source: str, old: str, new: str, label: str) -> str:
    n = source.count(old)
    if n != 1:
        raise RuntimeError(f'{label}: expected 1 occurrence, found {n}; upstream source may have changed')
    return source.replace(old, new, 1)


def make_modifications(root: Path) -> dict[Path, str]:
    cpp = root / 'source' / 'main.cpp'
    sysex = root / 'source' / 'sysex.cpp'
    strings = root / 'source' / 'strings.h'
    for p in (cpp, sysex, strings):
        if not p.is_file():
            raise RuntimeError(f'Missing original Volcako source: {p}')

    sx = sysex.read_text(encoding='utf-8')
    sx = replace_one(sx, OLD_SERIALISE, NEW_SERIALISE, 'operator-mute serialisation')
    sx = replace_one(sx, OLD_CHECKSUM, NEW_CHECKSUM, 'SysEx checksum')

    st = strings.read_text(encoding='utf-8')
    for old, new in (
        ('"DX7 Compatibility Mode"', '"Volca FM2 Mode"'),
        ('"DX7 mode is ON"', '"Volca FM2 mode is ON"'),
        ('"The Volca repurposes the DX7 checksum data to send the operator on/off status.\\nCheck this box to send the message in the intended DX7 format.\\nMost patches will sound wrong on the Volca when this is enabled."',
         '"Enable for Volca FM2: standard DX7 voice SysEx plus a muted-operator output-level workaround.\\nSwitch OFF for the original Volca FM. The MIDI message changes; saved settings remain compatible."'),
    ):
        st = replace_one(st, old, new, 'visible FM2 text')

    m = cpp.read_text(encoding='utf-8')
    # Insert after the existing drag_u8 helper, before next original function.
    insertion_anchor = '''// Update the operators' carrier status when algorithm changes'''
    m = replace_one(m, insertion_anchor, WHEEL_FUNCTIONS + insertion_anchor, 'wheel helper insertion')

    # drag_u8 must trigger all normal MIDI-dirty paths from its bool return.
    old_drag = '''    if (ImGui::DragInt(label, &tempval, 0.3f, min, max))
    {
        value = static_cast<u8>(tempval);
        return true;
    }
    return false;
'''
    new_drag = '''    const bool dragged = ImGui::DragInt(label, &tempval, 0.3f, min, max);
    const bool scrolled = wheel_adjust_int(tempval, min, max);
    if (dragged || scrolled)
    {
        value = static_cast<u8>(tempval);
        return true;
    }
    return false;
'''
    # Because the wheel helpers are defined after drag_u8 in upstream, we need a
    # forward declaration before drag_u8 (default args intentionally omitted).
    m = replace_one(m, '// imgui::dragint wrapper for u8 type',
        'static bool wheel_adjust_int(int& value, int min_value, int max_value);\n\n// imgui::dragint wrapper for u8 type', 'forward declaration')
    m = replace_one(m, old_drag, new_drag, 'drag_u8 wheel support')

    for old, new, label in (
        ('ImGui::DragInt4("R", v, 0.4f, 0, 99)', 'wheel_drag_int4("R", v, 0, 99)', 'envelope R'),
        ('ImGui::DragInt4("L", v, 0.4f, 0, 99)', 'wheel_drag_int4("L", v, 0, 99)', 'envelope L'),
        ('ImGui::SliderInt("FreqCoarse", &coarse, 0, 31)', 'wheel_slider_int("FreqCoarse", coarse, 0, 31)', 'FreqCoarse'),
        ('ImGui::SliderInt("FreqFine", &fine, 0, 99)', 'wheel_slider_int("FreqFine", fine, 0, 99)', 'FreqFine'),
        ('ImGui::SliderInt("Level", &level, 0, 99)', 'wheel_slider_int("Level", level, 0, 99)', 'Level'),
        ('ImGui::SliderInt("Detune", &detune, 0, 14)', 'wheel_slider_int("Detune", detune, 0, 14)', 'Detune'),
        ('ImGui::SliderInt("Break Point", &current_bp, 0, 99, oss.str().c_str())', 'wheel_slider_int("Break Point", current_bp, 0, 99, oss.str().c_str())', 'Break Point'),
        ('ImGui::Combo("L Curve", &item_current, LEVEL_SCALE_CURVE_NAMES, IM_ARRAYSIZE(LEVEL_SCALE_CURVE_NAMES))', 'wheel_combo_int("L Curve", item_current, LEVEL_SCALE_CURVE_NAMES, IM_ARRAYSIZE(LEVEL_SCALE_CURVE_NAMES))', 'L Curve'),
        ('ImGui::Combo("R Curve", &item_current, LEVEL_SCALE_CURVE_NAMES, IM_ARRAYSIZE(LEVEL_SCALE_CURVE_NAMES))', 'wheel_combo_int("R Curve", item_current, LEVEL_SCALE_CURVE_NAMES, IM_ARRAYSIZE(LEVEL_SCALE_CURVE_NAMES))', 'R Curve'),
        ('ImGui::Combo("Wave", &item_current, LFO_WAVE_NAMES, IM_ARRAYSIZE(LFO_WAVE_NAMES))', 'wheel_combo_int("Wave", item_current, LFO_WAVE_NAMES, IM_ARRAYSIZE(LFO_WAVE_NAMES))', 'Wave'),
        ('ImGui::SliderScalar("Feedback", ImGuiDataType_U8, &synth.feedback, &fb_min, &fb_max, "%d")', 'wheel_slider_u8("Feedback", synth.feedback, fb_min, fb_max, "%d")', 'Feedback'),
        ('ImGui::SliderScalar("Transpose", ImGuiDataType_U8, &synth.transpose, &transpo_min, &transpo_max, oss.str().c_str())', 'wheel_slider_u8("Transpose", synth.transpose, transpo_min, transpo_max, oss.str().c_str())', 'Transpose'),
        ('ImGui::SliderScalar("Algorithm", ImGuiDataType_U8, &synth.algorithm, &algo_min, &algo_max, "%d")', 'wheel_slider_u8("Algorithm", synth.algorithm, algo_min, algo_max, "%d")', 'Algorithm'),
    ):
        m = replace_one(m, old, new, label)

    return {sysex: sx, strings: st, cpp: m}


def patch_root(root: Path) -> None:
    # Validate ALL changes in memory first, so no partial patch if upstream shifts.
    updated = make_modifications(root)
    for dest, contents in updated.items():
        original = dest.read_text(encoding='utf-8')
        backup = dest.with_suffix(dest.suffix + '.original')
        if not backup.exists():
            backup.write_text(original, encoding='utf-8')
        dest.write_text(contents, encoding='utf-8')
        print('Patched:', dest)
    print('FM2 operator mute, mode rename, and wheel controls ready.')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise RuntimeError('Usage: python patch_volcako.py <volcako-source-folder>')
        patch_root(Path(sys.argv[1]))
    except Exception as exc:
        print('PATCH FAILED:', exc, file=sys.stderr)
        sys.exit(1)
