#!/usr/bin/env python3
"""Patch upstream Luomu/Volcako (master) for Korg Volca FM2.

Keeps the tested v1.1 FM2 MIDI and wheel edits, and adds UI scaling and
flexible operator-column layout (v1.2).

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


def make_modifications_v11(root: Path) -> dict[Path, str]:
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


# v1.2 visual-only changes. All v1.1 MIDI code above remains unmodified.
V12_LAYOUT = '        // FM2 v1.2: share the window width between patch library, operators and\n        // common controls. The operator columns fill the available centre pane.\n        const float available_panes_width = ImGui::GetContentRegionAvail().x;\n        const float left_pane_width = ui_px(180.f);\n        float right_pane_width = ui_px(310.f);\n        const float pane_spacing = ImGui::GetStyle().ItemSpacing.x * 2.f;\n        float patch_width = available_panes_width - left_pane_width - right_pane_width - pane_spacing;\n        // On narrower screens, prioritise space for the sound-design controls.\n        if (patch_width < ui_px(490.f))\n        {\n            right_pane_width = ImMax(ui_px(230.f), right_pane_width - (ui_px(490.f) - patch_width));\n            patch_width = available_panes_width - left_pane_width - right_pane_width - pane_spacing;\n        }\n        patch_width = ImMax(ui_px(320.f), patch_width);\n\n        if (ImGui::BeginChild("LeftPane", ImVec2(left_pane_width, 0), false))\n        {\n            if (ImGui::BeginChild("PatchList", ImVec2(0, 0), true))\n                layout_patch_list(app_state);\n            ImGui::EndChild();\n        }\n        ImGui::EndChild();\n\n        ImGui::SameLine();\n        if (ImGui::BeginChild("Patch", ImVec2(patch_width, 0), true))\n        {\n            SynthState& synth_state = app_state.get_current_patch();\n            ImGui::PushItemWidth(ui_px(80.f));\n            app_state.patch_dirty |= ImGui::InputText("Name", synth_state.patch_name, 10);\n            ImGui::PopItemWidth();\n\n            const float operator_available_width = ImGui::GetContentRegionAvail().x;\n            const int num_columns = operator_available_width >= ui_px(3.f * 275.f) ? 3 : 2;\n            ImGui::Columns(num_columns, "Operators", false);\n            const float column_width = operator_available_width / static_cast<float>(num_columns);\n            for (int i = 0; i < num_columns; ++i)\n                ImGui::SetColumnWidth(i, column_width);\n\n            if (num_columns == 3)\n            {\n                layout_operator(synth_state.op1, app_state);\n                ImGui::NextColumn();\n                layout_operator(synth_state.op2, app_state);\n                ImGui::NextColumn();\n                layout_operator(synth_state.op3, app_state);\n                ImGui::Separator();\n                ImGui::NextColumn();\n                layout_operator(synth_state.op4, app_state);\n                ImGui::NextColumn();\n                layout_operator(synth_state.op5, app_state);\n                ImGui::NextColumn();\n                layout_operator(synth_state.op6, app_state);\n            }\n            else\n            {\n                layout_operator(synth_state.op1, app_state);\n                ImGui::NextColumn();\n                layout_operator(synth_state.op2, app_state);\n                ImGui::Separator();\n                ImGui::NextColumn();\n                layout_operator(synth_state.op3, app_state);\n                ImGui::NextColumn();\n                layout_operator(synth_state.op4, app_state);\n                ImGui::Separator();\n                ImGui::NextColumn();\n                layout_operator(synth_state.op5, app_state);\n                ImGui::NextColumn();\n                layout_operator(synth_state.op6, app_state);\n            }\n            ImGui::Columns(1);\n        }\n        ImGui::EndChild();\n\n        ImGui::SameLine();\n        if (ImGui::BeginChild("RightPane", ImVec2(0, 0), false))\n            layout_common(app_state.get_current_patch(), app_state);\n        ImGui::EndChild();\n\n'


def make_modifications(root: Path) -> dict[Path, str]:
    updated = make_modifications_v11(root)
    main = root / 'source' / 'main.cpp'
    settings_cpp = root / 'source' / 'settings.cpp'
    settings_h = root / 'source' / 'settings.h'
    if not settings_cpp.is_file() or not settings_h.is_file():
        raise RuntimeError('Missing upstream Settings files')
    m = updated[main]
    m = replace_one(m, 'const float  FONT_SIZE     = 16.0f;',
        'const float  FONT_SIZE     = 16.0f;\nstatic float g_ui_scale = 1.0f;\nstatic float ui_px(float pixels) { return pixels * g_ui_scale; }', 'scale helper')
    m = replace_one(m, 'ImGui::PushItemWidth(250);',
        'ImGui::PushItemWidth(ImMax(ui_px(155.f), ImGui::GetContentRegionAvail().x - ui_px(28.f)));', 'envelope editor scaling')
    m = replace_one(m, 'ImGui::SameLine(60);', 'ImGui::SameLine(ui_px(60.f));', 'operator header spacing')
    m = replace_one(m, 'ImVec2(pos.x + 40.f, pos.y + ImGui::GetTextLineHeight()',
        'ImVec2(pos.x + ui_px(40.f), pos.y + ImGui::GetTextLineHeight()', 'operator colour tag scaling')
    m = replace_one(m, 'ImGui::PushItemWidth(150.f);',
        'ImGui::PushItemWidth(ImMax(ui_px(110.f), ImGui::GetContentRegionAvail().x - ui_px(133.f)));', 'operator slider width')
    for old, new, label in (
        ('ImGui::PushItemWidth(80.f);', 'ImGui::PushItemWidth(ui_px(80.f));', '80px input scaling'),
        ('ImGui::PushItemWidth(40.f);', 'ImGui::PushItemWidth(ui_px(40.f));', '40px input scaling'),
        ('ImGui::PushItemWidth(200.f);', 'ImGui::PushItemWidth(ui_px(200.f));', 'common pane scaling'),
    ):
        # These anchors appear in more than one place, so replace every occurrence.
        count = m.count(old)
        if not count:
            raise RuntimeError(f'{label}: no matching controls')
        m = m.replace(old, new)
    # Extend envelope drawings to use the current column, with their height scaled.
    m = replace_one(m, 'Envelope::draw_envelope("envelope", op.envelope, ImVec2(200.f, 75.f));',
        'Envelope::draw_envelope("envelope", op.envelope, ImVec2(ImMax(ui_px(170.f), ImGui::GetContentRegionAvail().x - ui_px(28.f)), ui_px(75.f)));', 'operator envelope width')
    m = replace_one(m, 'Envelope::draw_envelope("envelope", synth.pitch_env, ImVec2(200.f, 75.f));',
        'Envelope::draw_envelope("envelope", synth.pitch_env, ImVec2(ImMax(ui_px(170.f), ImGui::GetContentRegionAvail().x - ui_px(28.f)), ui_px(75.f)));', 'pitch envelope width')
    m = replace_one(m, 'const ImVec2 curpos     = ImGui::GetCursorScreenPos() + ImVec2(0.f, 25.f);',
        'const ImVec2 curpos     = ImGui::GetCursorScreenPos() + ImVec2(0.f, ui_px(25.f));', 'algorithm top spacing')
    m = replace_one(m, 'const float  op_width   = ALGO_BOX_SIZE;',
        'const float  op_width   = ui_px(ALGO_BOX_SIZE);', 'algorithm box scaling')
    m = replace_one(m, 'ImGui::BeginChild("Algoview", ImVec2(ALGO_BOX_SIZE * 12, ALGO_BOX_SIZE * 10), false);',
        'ImGui::BeginChild("Algoview", ImVec2(ui_px(ALGO_BOX_SIZE * 12), ui_px(ALGO_BOX_SIZE * 10)), false);', 'algorithm view scaling')
    m = replace_one(m, 'main_font = IO.Fonts->AddFontFromFileTTF(main_font_path.c_str(), FONT_SIZE);',
        'main_font = IO.Fonts->AddFontFromFileTTF(main_font_path.c_str(), ui_px(FONT_SIZE));', 'main font size')
    m = replace_one(m, 'icon_font               = IO.Fonts->AddFontFromFileTTF(icon_font_path.c_str(), 16.0f, &icons_config, icons_ranges);',
        'icon_font               = IO.Fonts->AddFontFromFileTTF(icon_font_path.c_str(), ui_px(16.0f), &icons_config, icons_ranges);', 'icon font size')
    m = replace_one(m, '    load_fonts(IO, app_state);',
        '    g_ui_scale = settings.get_ui_scale_percent() / 100.0f;\n    ImGui::GetStyle().ScaleAllSizes(g_ui_scale);\n    load_fonts(IO, app_state);', 'startup scale')
    layout_begin = '        const float left_pane_width       = 210.f;'
    layout_end = '        ImGui::End(); // main window'
    if m.count(layout_begin) != 1 or m.count(layout_end) != 1:
        raise RuntimeError('main window layout anchors missing / ambiguous')
    start = m.index(layout_begin)
    end = m.index(layout_end, start)
    m = m[:start] + V12_LAYOUT + m[end:]
    updated[main] = m

    h = settings_h.read_text(encoding='utf-8')
    h = replace_one(h, '    bool        dx7_compat_mode;',
        '    bool        dx7_compat_mode;\n    int         ui_scale_percent;', 'settings scale field')
    h = replace_one(h, '    bool               get_dx7_compat_mode() const { return dx7_compat_mode; }',
        '    int                get_ui_scale_percent() const { return ui_scale_percent; }\n    bool               get_dx7_compat_mode() const { return dx7_compat_mode; }', 'settings scale getter')
    updated[settings_h] = h

    s = settings_cpp.read_text(encoding='utf-8')
    s = replace_one(s, '    , dx7_compat_mode(false)',
        '    , dx7_compat_mode(false)\n    , ui_scale_percent(100)', 'default scale')
    s = replace_one(s, '        dx7_compat_mode   = sec["dx7_compat_mode"] == "true";',
        '        dx7_compat_mode   = sec["dx7_compat_mode"] == "true";\n'
        '        int saved_scale = 100;\n'
        '        inipp::extract(sec["ui_scale_percent"], saved_scale);\n'
        '        ui_scale_percent = saved_scale < 75 ? 75 : (saved_scale > 150 ? 150 : saved_scale);', 'read stored scale')
    s = replace_one(s, '    sec["dx7_compat_mode"] = dx7_compat_mode ? "true" : "false";',
        '    sec["dx7_compat_mode"] = dx7_compat_mode ? "true" : "false";\n'
        '    sec["ui_scale_percent"] = std::to_string(ui_scale_percent);', 'save scale')
    s = replace_one(s, '    if (ImGui::Button(Strings::BUTTON_CLOSE))',
        '    ImGui::Separator();\n'
        '    ImGui::TextUnformatted("Interface scale");\n'
        '    ImGui::SliderInt("##UI Scale", &ui_scale_percent, 75, 150, "%d%%");\n'
        '    ImGui::TextDisabled("Restart Volcako to apply a scale change.");\n\n'
        '    if (ImGui::Button(Strings::BUTTON_CLOSE))', 'options scale control')
    updated[settings_cpp] = s
    return updated


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
    print('Volcako FM2 v1.2: mute, wheel controls, responsive layout and scaling ready.')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise RuntimeError('Usage: python patch_volcako.py <volcako-source-folder>')
        patch_root(Path(sys.argv[1]))
    except Exception as exc:
        print('PATCH FAILED:', exc, file=sys.stderr)
        sys.exit(1)
