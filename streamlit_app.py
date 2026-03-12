"""
TraceQ — BOQ Risk Review Engine
================================
Streamlit web app for HVAC drawing analysis.
Upload a DXF/DWG drawing + BOQ spreadsheet → get a risk report.

Built by TechTelligence | nicholas@ttelligence.com
"""

import streamlit as st
import json
import os
import re
import io
import tempfile
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# Import the TraceQ engine (same directory)
from traceq_engine import TraceQEngine, Config, QuickScanResult, FileConverter


# ─── BOQ Parser ───────────────────────────────────────────────────────────────

# ─── BOQ Keyword Map ──────────────────────────────────────────────────────────
# Mapping: keywords in BOQ descriptions → engine equipment types
# Order matters — more specific matches first
BOQ_KEYWORD_MAP = [
    ('FCU-1', 'fcu', 'FCU-1 Ducted'),
    ('FCU-2', 'fcu', 'FCU-2 Ducted'),
    ('FCU-3', 'fcu', 'FCU-3 Ducted'),
    ('FCU-4', 'fcu', 'FCU-4'),
    ('FCU', 'fcu', 'FCU (General)'),
    ('FAN COIL', 'fcu', 'Fan Coil Unit'),
    ('THERMOSTAT', 'thermostat', 'Thermostat'),
    ('SUPPLY AIR DIFFUSER', 'supply_diffuser', 'Supply Air Diffuser'),
    ('RETURN AIR DIFFUSER', 'return_diffuser', 'Return Air Diffuser'),
    ('SUPPLY AIR FLOW BAR', 'flow_bar', 'Supply Air Flow Bar'),
    ('RETURN AIR FLOW BAR', 'flow_bar', 'Return Air Flow Bar'),
    ('FLOW BAR', 'flow_bar', 'Flow Bar'),
    ('PLENUM BOX', 'plenum_box', 'Plenum Box'),
    ('SUPPLY AIR VOLUME DAMPER', 'volume_control_damper', 'Supply Air Volume Damper'),
    ('VOLUME DAMPER', 'volume_control_damper', 'Volume Control Damper'),
    ('VCD', 'volume_control_damper', 'VCD'),
    ('FIRE DAMPER', 'fire_damper', 'Fire Damper'),
    ('MOTORIZED DAMPER', 'motorized_damper', 'Motorized Damper'),
    ('NON RETURN DAMPER', 'non_return_damper', 'Non-Return Damper'),
    ('SOUND ATTENUATOR', 'sound_attenuator', 'Sound Attenuator'),
    ('SUPPLY AIR DUCT', 'supply_duct', 'Supply Air Duct'),
    ('RETURN AIR DUCT', 'return_duct', 'Return Air Duct'),
    ('FLEXIBLE DUCT', 'flexible_duct', 'Flexible Duct'),
    ('VRV', 'vrf', 'VRV/VRF Unit'),
    ('VRF', 'vrf', 'VRF Unit'),
    ('OUTDOOR UNIT', 'outdoor_unit', 'Outdoor Unit'),
    ('INDOOR UNIT', 'indoor_unit', 'Indoor Unit'),
    ('WALL MOUNTED', 'indoor_unit', 'Wall Mounted Unit'),
    ('GRILLE', 'grille', 'Grille'),
    ('EXHAUST FAN', 'exhaust_fan', 'Exhaust Fan'),
    ('VENTILATION FAN', 'exhaust_fan', 'Ventilation Fan'),
    ('ACCESS DOOR', 'access_door', 'Access Door'),
    ('DRAIN', 'drain_pipe', 'Drain Pipe'),
    ('INSULATION', 'insulation', 'Insulation'),
]

# Units that can be compared directly (countable items)
COUNTABLE_UNITS = {'nos.', 'nos', 'no.', 'no', 'pcs', 'pcs.', 'ea', 'ea.', 'each', 'set', 'sets'}


def _detect_boq_columns(ws):
    """
    Detect which columns hold description, unit, qty, rate, total.
    Scans first 10 rows for header keywords, then validates against actual data.
    Returns dict with col indices (1-based).
    """
    cols = {'desc': 3, 'unit': 4, 'qty': 5, 'rate': 6, 'total': 7, 'item_no': 2}

    for row_num in range(1, min(11, ws.max_row + 1)):
        for col_num in range(1, min(11, ws.max_column + 1)):
            val = ws.cell(row=row_num, column=col_num).value
            if val is None:
                continue
            upper = str(val).strip().upper()
            if upper in ('UNIT', 'UOM', 'U/M'):
                cols['unit'] = col_num
            elif upper in ('QTY', 'QUANTITY', 'QTY.'):
                cols['qty'] = col_num
            elif 'RATE' in upper and 'TOTAL' not in upper:
                cols['rate'] = col_num
            elif 'TOTAL' in upper:
                cols['total'] = col_num

    text_col_counts = {}
    for row_num in range(5, min(20, ws.max_row + 1)):
        for col_num in range(1, min(8, ws.max_column + 1)):
            val = ws.cell(row=row_num, column=col_num).value
            if isinstance(val, str) and len(val.strip()) > 5:
                text_col_counts[col_num] = text_col_counts.get(col_num, 0) + 1

    if text_col_counts:
        best_col = max(text_col_counts, key=text_col_counts.get)
        cols['desc'] = best_col
        cols['item_no'] = best_col - 1 if best_col > 1 else 1

    return cols


def _classify_description(desc_text):
    upper = desc_text.upper().strip()
    for keyword, etype, label in BOQ_KEYWORD_MAP:
        if keyword in upper:
            return etype, label
    return None, desc_text


def parse_boq(file_bytes, filename):
    """
    Parse a BOQ Excel file and extract equipment line items.
    Uses column-position detection (not heuristic scanning).
    """
    suffix = os.path.splitext(filename)[1].lower() or '.xlsx'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        wb = openpyxl.load_workbook(tmp_path, data_only=True)
        ws = wb.active

        cols = _detect_boq_columns(ws)
        items = []
        item_counter = 0

        for row_num in range(1, ws.max_row + 1):
            item_no_val = ws.cell(row=row_num, column=cols['item_no']).value
            desc_val = ws.cell(row=row_num, column=cols['desc']).value
            unit_val = ws.cell(row=row_num, column=cols['unit']).value
            qty_val = ws.cell(row=row_num, column=cols['qty']).value
            rate_val = ws.cell(row=row_num, column=cols['rate']).value
            total_val = ws.cell(row=row_num, column=cols['total']).value

            if desc_val is None:
                continue
            desc_str = str(desc_val).strip()
            if len(desc_str) < 3:
                continue
            if qty_val is None:
                continue
            try:
                qty = float(qty_val)
            except (ValueError, TypeError):
                continue
            if qty <= 0:
                continue

            equip_type, equip_label = _classify_description(desc_str)
            unit_str = str(unit_val).strip() if unit_val else None
            try:
                rate = float(rate_val) if rate_val else None
            except (ValueError, TypeError):
                rate = None
            try:
                total = float(total_val) if total_val else None
            except (ValueError, TypeError):
                total = None

            item_counter += 1
            boq_ref = str(item_no_val).strip() if item_no_val else str(item_counter)

            items.append({
                'description': desc_str,
                'equipment_type': equip_type,
                'equipment_label': equip_label,
                'qty': qty,
                'unit': unit_str,
                'rate': rate,
                'total': total,
                'boq_ref': boq_ref,
                'is_countable': (unit_str or '').lower().strip('.') in {'nos', 'no', 'pcs', 'ea', 'each', 'set', 'sets'},
            })

        os.unlink(tmp_path)
        return items

    except Exception as e:
        os.unlink(tmp_path)
        raise e


def compare_boq_vs_drawing(boq_items, drawing_merged):
    """
    Compare BOQ line items against drawing detection results.
    Returns (comparisons, missing_from_boq) with Trace IDs assigned.
    """
    comparisons = []
    trace_counter = 0

    # Group BOQ items by equipment type
    boq_by_type = {}
    for item in boq_items:
        etype = item['equipment_type']
        if etype is None:
            continue
        if etype not in boq_by_type:
            boq_by_type[etype] = {
                'total_qty': 0,
                'items': [],
                'total_cost': 0,
                'rates': [],
                'units': set(),
            }
        boq_by_type[etype]['total_qty'] += item['qty']
        boq_by_type[etype]['items'].append(item)
        if item.get('total'):
            boq_by_type[etype]['total_cost'] += item['total']
        if item.get('rate') and item['rate'] > 0:
            boq_by_type[etype]['rates'].append(item['rate'])
        if item.get('unit'):
            boq_by_type[etype]['units'].add(item['unit'].strip().lower())

    # Build comparison for each BOQ equipment type
    matched_drawing_types = set()

    for etype in sorted(boq_by_type.keys()):
        boq_data = boq_by_type[etype]
        drawing_data = drawing_merged.get(etype, {})
        matched_drawing_types.add(etype)

        boq_qty = boq_data['total_qty']
        drawing_qty = drawing_data.get('count', 0)
        source = drawing_data.get('source', '—')
        rates = boq_data['rates']
        avg_rate = sum(rates) / len(rates) if rates else 0
        units = boq_data['units']

        has_non_countable = any(u.strip('.') not in ('nos', 'no', 'pcs', 'ea', 'each', 'set', 'sets') for u in units)

        diff = drawing_qty - boq_qty if drawing_qty > 0 else 0
        exposure = abs(diff) * avg_rate if avg_rate and drawing_qty > 0 else 0

        # Determine risk level
        if has_non_countable and drawing_qty == 0:
            risk = 'VERIFY'
            note = _build_verify_note(etype, boq_data, units)
        elif drawing_qty == 0 and boq_qty > 0:
            risk = 'VERIFY'
            note = f"BOQ has {int(boq_qty)} but not detected in drawing. May require manual check."
        elif diff == 0:
            risk = 'MATCH'
            note = "Quantities match."
        elif has_non_countable:
            risk = 'VERIFY'
            note = _build_verify_note(etype, boq_data, units)
        elif abs(diff) / max(boq_qty, 1) > 0.2:
            risk = 'HIGH'
            note = _build_discrepancy_note(etype, boq_data, drawing_data, diff)
        elif abs(diff) / max(boq_qty, 1) > 0.1:
            risk = 'MEDIUM'
            note = _build_discrepancy_note(etype, boq_data, drawing_data, diff)
        elif diff != 0:
            risk = 'LOW'
            note = _build_discrepancy_note(etype, boq_data, drawing_data, diff)
        else:
            risk = 'MATCH'
            note = "Quantities match."

        boq_breakdown = _format_boq_breakdown(boq_data['items'])
        name = _format_equipment_name(etype)
        source_label = _format_source_label(source)

        # For VERIFY items with non-countable units, don't show misleading diff/exposure
        is_verify_non_countable = risk == 'VERIFY' and has_non_countable
        if is_verify_non_countable:
            show_diff = '—'
            show_exposure = '—'
        else:
            show_diff = f"{int(diff):+d}" if drawing_qty > 0 else '—'
            show_exposure = f"{exposure:,.0f}" if exposure > 0 else '—'

        show_drawing_qty = int(drawing_qty) if drawing_qty > 0 else '—'

        # Assign trace ID
        trace_counter += 1
        trace_id = f"TQ-{trace_counter:03d}"

        comparisons.append({
            'Trace ID': trace_id,
            'Equipment': name,
            'BOQ Qty': int(boq_qty) if boq_qty == int(boq_qty) else f"{boq_qty:,.1f}",
            'Drawing Qty': show_drawing_qty,
            'Difference': show_diff,
            'Unit': ', '.join(sorted(units)) if units else '—',
            'Risk': risk,
            'Exposure (AED)': show_exposure,
            'Notes': note,
            'BOQ Breakdown': boq_breakdown,
            'Detection Source': source_label,
            '_exposure_num': exposure if not is_verify_non_countable else None,
            '_boq_qty': boq_qty,
            '_drawing_qty': drawing_qty if drawing_qty > 0 else None,
            '_diff': diff if not is_verify_non_countable else None,
            '_rate': avg_rate,
        })

    # Missing from BOQ: items in drawing but not in any BOQ type
    missing_from_boq = []
    for etype, data in sorted(drawing_merged.items()):
        if etype not in matched_drawing_types and data.get('count', 0) > 0:
            trace_counter += 1
            trace_id = f"TQ-{trace_counter:03d}"
            name = _format_equipment_name(etype)
            source = data.get('source', 'unknown')
            source_label = _format_source_label(source)
            confidence = data.get('confidence', 0)
            missing_from_boq.append({
                'Trace ID': trace_id,
                'Equipment': name,
                'Drawing Qty': data['count'],
                'Detection': source_label,
                'Confidence': f"{int(confidence * 100)}%",
                'Notes': f"Found in drawing via {source_label.lower()} but no matching BOQ line item.",
            })

    return comparisons, missing_from_boq


def _format_equipment_name(etype):
    """Format equipment type to proper display name, preserving acronyms."""
    acronyms = {'fcu': 'FCU', 'vrf': 'VRF', 'vcd': 'VCD', 'sad': 'SAD', 'rad': 'RAD'}
    name = etype.replace('_', ' ').title()
    for key, acr in acronyms.items():
        name = name.replace(key.title(), acr)
    return name


def _format_source_label(source):
    """Convert raw engine source to clean label."""
    if not source or source == '—':
        return '—'
    if 'tier1' in source:
        return 'Layer Detection'
    elif 'tier2' in source:
        return 'Block Detection'
    elif 'tier3' in source or 'SAD' in source:
        return 'Text/Label Detection'
    return source


def _format_boq_breakdown(items):
    if len(items) == 1:
        return items[0]['description'][:60]
    parts = []
    for item in items:
        desc_short = item['description'][:40]
        parts.append(f"{desc_short}: {int(item['qty']) if item['qty'] == int(item['qty']) else item['qty']}")
    return " | ".join(parts)


def _build_discrepancy_note(etype, boq_data, drawing_data, diff):
    drawing_qty = drawing_data.get('count', 0)
    boq_qty = boq_data['total_qty']
    source = drawing_data.get('source', 'detection')

    if 'tier1' in source:
        source_label = "layer detection"
    elif 'tier2' in source:
        source_label = "block detection"
    elif 'tier3' in source or 'SAD' in source:
        source_label = "text/label detection"
    else:
        source_label = source

    if diff > 0:
        note = f"Drawing shows {int(drawing_qty)} via {source_label} — {abs(int(diff))} more than BOQ ({int(boq_qty)})."
    else:
        note = f"Drawing shows {int(drawing_qty)} via {source_label} — {abs(int(diff))} fewer than BOQ ({int(boq_qty)})."

    if len(boq_data['items']) > 1:
        sub_parts = []
        for item in boq_data['items']:
            short = item['description'][:35]
            sub_parts.append(f"{short}={int(item['qty'])}")
        note += f" BOQ breakdown: {', '.join(sub_parts)}."

    if etype == 'return_diffuser' and diff > 0:
        note += " Note: block detection may double-count *U16/*U17 inserts — verify against drawing legend."
    elif etype == 'vrf' and diff < 0:
        note += " Note: drawing detects unique VRF labels only — BOQ may list individual modules per system."
    elif etype == 'flow_bar' and diff < 0:
        note += " Note: flow bars often lack distinct block markers — text detection may undercount."
    elif etype == 'volume_control_damper' and len(boq_data['items']) > 1:
        note += " Note: BOQ may list different damper sizes separately — verify each size against drawing."

    return note


def _build_verify_note(etype, boq_data, units):
    unit_list = ', '.join(sorted(units))
    boq_qty = boq_data['total_qty']

    if any(u.strip('.') in ('sqm', 'sq.m', 'sq m', 'm2', 'sqft') for u in units):
        return f"BOQ quantity is in area ({unit_list}) = {boq_qty:,.1f}. Cannot compare directly with drawing count. Manual verification required."
    elif any(u.strip('.') in ('mtrs', 'mtr', 'm', 'lm', 'rm') for u in units):
        return f"BOQ quantity is in length ({unit_list}) = {boq_qty:,.1f}. Cannot compare directly with drawing count. Manual verification required."
    else:
        return f"Unit mismatch ({unit_list}). BOQ qty = {boq_qty:,.1f}. Needs human review."


# ─── Excel Report Generator ──────────────────────────────────────────────────

def _xl_val(v):
    """Return '—' string for None values in Excel cells."""
    return '—' if v is None else v


def generate_nestor_feedback(comparisons, missing_from_boq, merged, drawing_name, scan=None):
    """
    Auto-generate a QS feedback sheet merging Step 0 + BOQ results.
    One tab, three columns: What TraceQ Found | Y/N | Comment.
    Sections: BOQ Comparison → Unknown Blocks/Layers → Quick Questions.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Feedback"
    ws.sheet_properties.tabColor = "1A1A2E"

    # Styles
    title_font = Font(name='Arial', bold=True, size=14, color='1A1A2E')
    sub_font = Font(name='Arial', size=10, color='666666')
    hdr_font = Font(name='Arial', bold=True, size=11, color='FFFFFF')
    hdr_fill = PatternFill('solid', fgColor='1A1A2E')
    input_hdr_fill = PatternFill('solid', fgColor='B8860B')
    section_font = Font(name='Arial', bold=True, size=11, color='1A1A2E')
    section_fill = PatternFill('solid', fgColor='E8E8E8')
    bold_font = Font(name='Arial', bold=True, size=10)
    normal_font = Font(name='Arial', size=10)
    match_font = Font(name='Arial', size=10, color='28A745')
    issue_font = Font(name='Arial', size=10, color='DC3545', bold=True)
    q_font = Font(name='Arial', size=10, color='0066CC', bold=True)
    gray_font = Font(name='Arial', size=9, color='999999')
    input_fill = PatternFill('solid', fgColor='FFF9E6')
    thin_border = Border(
        left=Side(style='thin', color='DDDDDD'),
        right=Side(style='thin', color='DDDDDD'),
        top=Side(style='thin', color='DDDDDD'),
        bottom=Side(style='thin', color='DDDDDD'),
    )

    drawing_short = drawing_name.replace('.dxf', '').replace('.DXF', '').replace('.dwg', '').replace('.DWG', '')

    # Header
    ws.merge_cells('A1:C1')
    ws['A1'] = f'TraceQ Feedback — {drawing_short}'
    ws['A1'].font = title_font
    ws.merge_cells('A2:C2')
    ws['A2'] = 'Type Y or N for each item. Only add a comment if you mark N. Should take 5-10 mins.'
    ws['A2'].font = sub_font

    # Column headers
    for col_letter, header, width, fill in [
        ('A', 'TraceQ Result', 60, hdr_fill),
        ('B', 'Y / N', 10, input_hdr_fill),
        ('C', 'Comment (only if N)', 45, input_hdr_fill),
    ]:
        c = ws[f'{col_letter}4']
        c.value = header
        c.font = hdr_font
        c.fill = fill
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.border = thin_border
        ws.column_dimensions[col_letter].width = width
    ws.row_dimensions[4].height = 28

    def _add_row(ws, row, text, font, hint=None, row_height=28):
        """Helper to add a standard feedback row."""
        ws.cell(row, 1, text).font = font
        ws.cell(row, 1).border = thin_border
        ws.cell(row, 1).alignment = Alignment(vertical='center', wrap_text=True)
        ws.cell(row, 2).fill = input_fill
        ws.cell(row, 2).font = bold_font
        ws.cell(row, 2).alignment = Alignment(horizontal='center', vertical='center')
        ws.cell(row, 2).border = thin_border
        ws.cell(row, 3).fill = input_fill
        ws.cell(row, 3).font = normal_font
        ws.cell(row, 3).alignment = Alignment(vertical='center', wrap_text=True)
        ws.cell(row, 3).border = thin_border
        if hint:
            ws.cell(row, 3, hint).font = gray_font
        ws.row_dimensions[row].height = row_height

    def _add_section(ws, row, title):
        """Helper to add a section header."""
        ws.cell(row, 1, title).font = section_font
        for col in range(1, 4):
            ws.cell(row, col).fill = section_fill
            ws.cell(row, col).border = thin_border
        ws.merge_cells(f'A{row}:C{row}')
        ws.row_dimensions[row].height = 30

    row = 5

    # ═══ SECTION 1: BOQ COMPARISON ═══
    _add_section(ws, row, 'BOQ COMPARISON — Is TraceQ\'s count correct?')
    row += 1

    for comp in comparisons:
        equip = comp['Equipment']
        risk = comp['Risk']
        boq_qty = comp.get('_boq_qty', 0)
        dwg_qty = comp.get('_drawing_qty')

        boq_str = f"{boq_qty:,}" if isinstance(boq_qty, (int, float)) else str(boq_qty)
        dwg_str = f"{dwg_qty:,}" if isinstance(dwg_qty, (int, float)) and dwg_qty else str(dwg_qty or '—')

        if risk == 'MATCH':
            text = f"{equip}:  TraceQ = {dwg_str},  BOQ = {boq_str}"
            font = match_font
            hint = None
        elif risk == 'VERIFY':
            unit = comp.get('Unit', '')
            if dwg_qty and dwg_qty != '—':
                text = f"{equip}:  TraceQ = {dwg_str},  BOQ = {boq_str} {unit}"
            else:
                text = f"{equip}:  not detected,  BOQ = {boq_str} {unit}"
            font = normal_font
            hint = f"BOQ in {unit} — can't compare directly" if unit and unit != 'nos.' else None
        elif risk in ('HIGH', 'MEDIUM', 'LOW'):
            text = f"{equip}:  TraceQ = {dwg_str},  BOQ = {boq_str}"
            font = issue_font if risk == 'HIGH' else normal_font
            hint = None
        else:
            continue

        # Add review hint if tiers disagree
        equip_key = equip.lower().replace(' ', '_')
        merged_data = merged.get(equip_key, {})
        if merged_data.get('needs_review'):
            alt = merged_data.get('alternate_counts', {})
            hint = f"Tiers differ: Layer={alt.get('tier1', 0)}, Block={alt.get('tier2', 0)}, Text={alt.get('tier3', 0)}"

        _add_row(ws, row, text, font, hint)
        row += 1

    # Missing from BOQ
    for m in missing_from_boq:
        equip = m['Equipment']
        qty = m['Drawing Qty']
        qty_str = f"{qty:,}" if isinstance(qty, (int, float)) else str(qty)
        text = f"{equip}:  TraceQ found {qty_str}  (not in BOQ — should it be?)"
        _add_row(ws, row, text, normal_font)
        row += 1

    # ═══ SECTION 2: UNKNOWN BLOCKS & LAYERS (from Step 0) ═══
    has_unknowns = False
    if scan:
        unrec_blocks = getattr(scan, 'unrecognised_blocks', []) or []
        unrec_layers = getattr(scan, 'unrecognised_layers', []) or []
        rec_blocks = getattr(scan, 'recognised_blocks', []) or []

        # Filter: only show unknown blocks with significant count (>5 occurrences)
        sig_blocks = [ub for ub in unrec_blocks if ub.get('count', 0) > 5]
        # Filter: only show unknown layers that look HVAC-related
        hvac_hints = ['HVAC', 'AC', 'DUCT', 'DIFF', 'VCD', 'FCU', 'FAN', 'DAMPER', 'PIPE', 'REF', 'THERM', 'VRF', 'VRV', 'AHU']
        sig_layers = [ul for ul in unrec_layers if any(h in ul.upper() for h in hvac_hints)]

        if sig_blocks or sig_layers:
            has_unknowns = True
            row += 1
            _add_section(ws, row, 'UNKNOWN ITEMS — Is this equipment? (Y = yes, N = not equipment)')
            row += 1

            for ub in sig_blocks:
                block_name = ub.get('block', ub) if isinstance(ub, dict) else str(ub)
                count = ub.get('count', '?') if isinstance(ub, dict) else '?'
                text = f"Block \"{block_name}\" ({count} occurrences) — is this equipment?"
                _add_row(ws, row, text, normal_font, hint="If Y, what equipment type?")
                row += 1

            for ul in sig_layers:
                layer_name = ul if isinstance(ul, str) else str(ul)
                text = f"Layer \"{layer_name}\" — is this an equipment layer?"
                _add_row(ws, row, text, normal_font, hint="If Y, what equipment type?")
                row += 1

    # ═══ SECTION 3: QUICK QUESTIONS (only for genuinely ambiguous items) ═══
    auto_questions = []
    for comp in comparisons:
        risk = comp['Risk']
        equip = comp['Equipment']
        equip_key = equip.lower().replace(' ', '_')
        merged_data = merged.get(equip_key, {})

        if merged_data.get('needs_review'):
            alt = merged_data.get('alternate_counts', {})
            auto_questions.append(
                f"{equip}: Layer={alt.get('tier1', 0)}, Block={alt.get('tier2', 0)}, "
                f"Text={alt.get('tier3', 0)} — which count is correct?"
            )
        elif risk == 'HIGH':
            dwg = comp.get('_drawing_qty', '?')
            boq = comp.get('_boq_qty', '?')
            auto_questions.append(
                f"{equip}: TraceQ found {dwg} but BOQ has {boq} — why the gap?"
            )

    if auto_questions:
        row += 1
        _add_section(ws, row, 'QUESTIONS (short answer needed)')
        row += 1

        for q in auto_questions:
            ws.cell(row, 1, q).font = q_font
            ws.cell(row, 1).border = thin_border
            ws.cell(row, 1).alignment = Alignment(vertical='center', wrap_text=True)
            ws.merge_cells(f'B{row}:C{row}')
            ws.cell(row, 2).fill = input_fill
            ws.cell(row, 2).font = normal_font
            ws.cell(row, 2).alignment = Alignment(vertical='center', wrap_text=True)
            ws.cell(row, 2).border = thin_border
            ws.cell(row, 3).border = thin_border
            ws.row_dimensions[row].height = 40
            row += 1

    ws.freeze_panes = 'A5'

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


def generate_excel_report(comparisons, missing_from_boq, boq_items, drawing_name, boq_name, merged=None, dedup_report=None):
    """
    Generate a professional Excel BOQ Discrepancy Report with 4 tabs:
      Tab 1: Executive Summary
      Tab 2: Discrepancy Details
      Tab 3: Items Not in BOQ
      Tab 4: Detection Audit (three-tier counts, review flags, dedup)
    Returns bytes of the .xlsx file.
    """
    wb = openpyxl.Workbook()

    # Styles
    header_font = Font(name='Arial', bold=True, size=11, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1A1A2E')
    title_font = Font(name='Arial', bold=True, size=16, color='1A1A2E')
    subtitle_font = Font(name='Arial', bold=False, size=10, color='666666')
    bold_font = Font(name='Arial', bold=True, size=10)
    bold_font_big = Font(name='Arial', bold=True, size=11)
    normal_font = Font(name='Arial', size=10)
    match_fill = PatternFill('solid', fgColor='D4EDDA')
    high_fill = PatternFill('solid', fgColor='F8D7DA')
    high_font = Font(name='Arial', size=10, bold=True, color='DC3545')
    medium_fill = PatternFill('solid', fgColor='FFF3CD')
    low_fill = PatternFill('solid', fgColor='D1ECF1')
    verify_fill = PatternFill('solid', fgColor='E8DAEF')
    missing_fill = PatternFill('solid', fgColor='FADBD8')
    section_fill = PatternFill('solid', fgColor='F0F0F0')
    thin_border = Border(
        left=Side(style='thin', color='CCCCCC'),
        right=Side(style='thin', color='CCCCCC'),
        top=Side(style='thin', color='CCCCCC'),
        bottom=Side(style='thin', color='CCCCCC'),
    )

    risk_fills = {
        'MATCH': match_fill,
        'HIGH': high_fill,
        'MEDIUM': medium_fill,
        'LOW': low_fill,
        'VERIFY': verify_fill,
    }

    now = datetime.now().strftime('%d %B %Y, %H:%M')

    # ─── Tab 1: Executive Summary ─────────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Executive Summary"
    ws1.sheet_properties.tabColor = "1A1A2E"

    ws1['A1'] = 'TraceQ — BOQ Discrepancy Report'
    ws1['A1'].font = title_font
    ws1.merge_cells('A1:F1')
    ws1['A2'] = f'Generated: {now}'
    ws1['A2'].font = subtitle_font
    ws1['A3'] = f'Drawing: {drawing_name}'
    ws1['A3'].font = subtitle_font
    ws1['A4'] = f'BOQ: {boq_name}'
    ws1['A4'].font = subtitle_font
    ws1['A5'] = 'Prepared by: TechTelligence | TraceQ Engine v1.0'
    ws1['A5'].font = subtitle_font

    # Summary stats
    matches = sum(1 for c in comparisons if c['Risk'] == 'MATCH')
    discrepancies = sum(1 for c in comparisons if c['Risk'] in ('HIGH', 'MEDIUM', 'LOW'))
    verify_count = sum(1 for c in comparisons if c['Risk'] == 'VERIFY')
    missing_count = len(missing_from_boq)
    total_exposure = sum(c.get('_exposure_num', 0) or 0 for c in comparisons)

    row = 7
    c = ws1.cell(row=row, column=1, value='SUMMARY')
    c.font = bold_font_big
    c.fill = section_fill
    for ci in range(1, 7):
        ws1.cell(row=row, column=ci).fill = section_fill
    row += 1

    labels = [
        ('Items Matching', matches, '#,##0'),
        ('Discrepancies Found', discrepancies, '#,##0'),
        ('Items Needing Verification', verify_count, '#,##0'),
        ('Items Missing from BOQ', missing_count, '#,##0'),
        ('Total Quantifiable Exposure (AED)', total_exposure, '#,##0'),
    ]
    for label, val, fmt in labels:
        ws1.cell(row=row, column=1, value=label).font = normal_font
        c = ws1.cell(row=row, column=2, value=val)
        c.font = bold_font
        c.number_format = fmt
        row += 1

    # Risk breakdown table
    row += 1
    c = ws1.cell(row=row, column=1, value='RISK BREAKDOWN')
    c.font = bold_font_big
    c.fill = section_fill
    for ci in range(1, 7):
        ws1.cell(row=row, column=ci).fill = section_fill
    row += 1

    risk_headers = ['Trace ID', 'Equipment', 'Risk', 'BOQ Qty', 'Drawing Qty', 'Exposure (AED)']
    for col_idx, h in enumerate(risk_headers, 1):
        c = ws1.cell(row=row, column=col_idx, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center')
        c.border = thin_border
    row += 1

    for comp in comparisons:
        if comp['Risk'] == 'MATCH':
            continue
        boq_val = _xl_val(comp.get('_boq_qty'))
        dwg_val = _xl_val(comp.get('_drawing_qty'))
        exp_val = _xl_val(comp.get('_exposure_num'))

        vals = [comp['Trace ID'], comp['Equipment'], comp['Risk'], boq_val, dwg_val, exp_val]
        fill = risk_fills.get(comp['Risk'], None)
        for col_idx, v in enumerate(vals, 1):
            c = ws1.cell(row=row, column=col_idx, value=v)
            c.font = normal_font
            c.border = thin_border
            if fill:
                c.fill = fill
            if col_idx in (4, 5) and isinstance(v, (int, float)):
                c.number_format = '#,##0'
            if col_idx == 6 and isinstance(v, (int, float)):
                c.number_format = '#,##0'
            c.alignment = Alignment(horizontal='center') if col_idx != 2 else Alignment(horizontal='left')
        row += 1

    if missing_from_boq:
        for m in missing_from_boq:
            vals = [m['Trace ID'], m['Equipment'], 'MISSING FROM BOQ', '—', m['Drawing Qty'], '—']
            for col_idx, v in enumerate(vals, 1):
                c = ws1.cell(row=row, column=col_idx, value=v)
                c.font = normal_font
                c.border = thin_border
                c.fill = missing_fill
                if col_idx == 5 and isinstance(v, (int, float)):
                    c.number_format = '#,##0'
                c.alignment = Alignment(horizontal='center') if col_idx != 2 else Alignment(horizontal='left')
            row += 1

    ws1.column_dimensions['A'].width = 35
    ws1.column_dimensions['B'].width = 28
    ws1.column_dimensions['C'].width = 20
    ws1.column_dimensions['D'].width = 14
    ws1.column_dimensions['E'].width = 14
    ws1.column_dimensions['F'].width = 18

    # ─── Tab 2: Discrepancy Details ───────────────────────────────────────────
    ws2 = wb.create_sheet("Discrepancy Details")
    ws2.sheet_properties.tabColor = "DC3545"

    ws2['A1'] = 'BOQ vs Drawing — Detailed Comparison'
    ws2['A1'].font = title_font
    ws2.merge_cells('A1:K1')
    ws2['A2'] = f'Drawing: {drawing_name} | BOQ: {boq_name}'
    ws2['A2'].font = subtitle_font

    headers = ['Trace ID', 'Equipment', 'BOQ Qty', 'Drawing Qty', 'Difference', 'Unit', 'Risk', 'Exposure (AED)', 'Detection Source', 'Notes', 'BOQ Breakdown']
    row = 4
    for col_idx, h in enumerate(headers, 1):
        c = ws2.cell(row=row, column=col_idx, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center', wrap_text=True)
        c.border = thin_border
    row += 1

    for comp in comparisons:
        boq_val = comp.get('_boq_qty', 0)
        dwg_val = _xl_val(comp.get('_drawing_qty'))
        diff_val = _xl_val(comp.get('_diff'))
        exp_val = _xl_val(comp.get('_exposure_num'))

        vals = [
            comp['Trace ID'], comp['Equipment'], boq_val, dwg_val, diff_val,
            comp['Unit'], comp['Risk'], exp_val,
            comp.get('Detection Source', '—'), comp['Notes'], comp.get('BOQ Breakdown', ''),
        ]
        fill = risk_fills.get(comp['Risk'], None)
        for col_idx, v in enumerate(vals, 1):
            c = ws2.cell(row=row, column=col_idx, value=v)
            c.font = normal_font
            c.border = thin_border
            if fill and col_idx == 7:
                c.fill = fill
            if col_idx in (3, 4, 5) and isinstance(v, (int, float)):
                c.number_format = '#,##0'
            if col_idx == 8 and isinstance(v, (int, float)):
                c.number_format = '#,##0'
            if col_idx in (10, 11):
                c.alignment = Alignment(wrap_text=True, vertical='top')
            elif col_idx in (1, 3, 4, 5, 6, 7, 8):
                c.alignment = Alignment(horizontal='center')
        row += 1

    detail_widths = [12, 28, 12, 14, 12, 8, 12, 16, 20, 60, 50]
    for i, w in enumerate(detail_widths):
        ws2.column_dimensions[get_column_letter(i + 1)].width = w

    # ─── Tab 3: Items Not in BOQ ──────────────────────────────────────────────
    ws3 = wb.create_sheet("Not in BOQ")
    ws3.sheet_properties.tabColor = "FD7E14"

    ws3['A1'] = 'Items Detected in Drawing — Not in BOQ'
    ws3['A1'].font = title_font
    ws3.merge_cells('A1:F1')
    ws3['A2'] = 'These items were found in the drawing but have no corresponding BOQ line item. They may represent scope gaps or items not yet priced.'
    ws3['A2'].font = subtitle_font
    ws3.merge_cells('A2:F2')

    headers3 = ['Trace ID', 'Equipment', 'Drawing Qty', 'Detection Method', 'Confidence', 'Notes']
    row = 4
    for col_idx, h in enumerate(headers3, 1):
        c = ws3.cell(row=row, column=col_idx, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center')
        c.border = thin_border
    row += 1

    if missing_from_boq:
        for m in missing_from_boq:
            vals = [m['Trace ID'], m['Equipment'], m['Drawing Qty'], m['Detection'], m['Confidence'], m['Notes']]
            for col_idx, v in enumerate(vals, 1):
                c = ws3.cell(row=row, column=col_idx, value=v)
                c.font = normal_font
                c.border = thin_border
                c.fill = missing_fill
                if col_idx == 3 and isinstance(v, (int, float)):
                    c.number_format = '#,##0'
                if col_idx == 6:
                    c.alignment = Alignment(wrap_text=True, vertical='top')
                else:
                    c.alignment = Alignment(horizontal='center') if col_idx != 2 else Alignment(horizontal='left')
            row += 1
    else:
        ws3.cell(row=row, column=1, value='No items missing from BOQ.').font = normal_font

    not_in_boq_widths = [12, 28, 14, 20, 12, 60]
    for i, w in enumerate(not_in_boq_widths):
        ws3.column_dimensions[get_column_letter(i + 1)].width = w

    # ─── Tab 4: Detection Audit ──────────────────────────────────────────────
    if merged:
        ws4 = wb.create_sheet("Detection Audit")
        ws4.sheet_properties.tabColor = "0066CC"

        ws4['A1'] = 'Detection Tier Audit — Three-Tier Breakdown'
        ws4['A1'].font = title_font
        ws4.merge_cells('A1:H1')
        ws4['A2'] = 'Shows what each detection tier found. Review flags indicate significant disagreements requiring QS verification.'
        ws4['A2'].font = subtitle_font
        ws4.merge_cells('A2:H2')

        review_fill = PatternFill('solid', fgColor='FFF3CD')

        headers4 = ['Equipment', 'Final Count', 'Source', 'Confidence', 'Layer (T1)', 'Block (T2)', 'Text (T3)', 'Status']
        row = 4
        for col_idx, h in enumerate(headers4, 1):
            c = ws4.cell(row=row, column=col_idx, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal='center')
            c.border = thin_border
        row += 1

        for equip_type in sorted(merged.keys()):
            data = merged[equip_type]
            alt = data.get('alternate_counts', {})
            source = data.get('source', '')
            if 'tier1' in source:
                src_label = 'Layer'
            elif 'tier2' in source:
                src_label = 'Block'
            elif 'tier3' in source:
                src_label = 'Text'
            else:
                src_label = source

            flagged = data.get('needs_review', False)
            t1 = alt.get('tier1', 0)
            t2 = alt.get('tier2', 0)
            t3 = alt.get('tier3', 0)

            vals = [
                _format_equipment_name(equip_type),
                data.get('count', 0),
                src_label,
                f"{int(data.get('confidence', 0) * 100)}%",
                t1 if t1 > 0 else '—',
                t2 if t2 > 0 else '—',
                t3 if t3 > 0 else '—',
                'REVIEW' if flagged else 'OK',
            ]
            for col_idx, v in enumerate(vals, 1):
                c = ws4.cell(row=row, column=col_idx, value=v)
                c.font = normal_font
                c.border = thin_border
                if flagged:
                    c.fill = review_fill
                if col_idx in (2, 5, 6, 7) and isinstance(v, (int, float)):
                    c.number_format = '#,##0'
                c.alignment = Alignment(horizontal='center') if col_idx != 1 else Alignment(horizontal='left')
            row += 1

        # Dedup section
        if dedup_report:
            adjustments = dedup_report.get('adjustments', [])
            if adjustments:
                row += 1
                ws4.cell(row=row, column=1, value='PROXIMITY DEDUPLICATION').font = bold_font_big
                ws4.cell(row=row, column=1).fill = section_fill
                for ci in range(1, 9):
                    ws4.cell(row=row, column=ci).fill = section_fill
                row += 1
                ws4.cell(row=row, column=1, value=f"Radius: {dedup_report.get('radius_used', 0):.0f} units").font = normal_font
                row += 1
                for adj in adjustments:
                    ws4.cell(row=row, column=1, value=_format_equipment_name(adj.get('equipment_type', ''))).font = normal_font
                    ws4.cell(row=row, column=2, value=f"T3: {adj.get('tier3_original', 0)} → {adj.get('tier3_adjusted', 0)}").font = normal_font
                    ws4.cell(row=row, column=3, value=f"-{adj.get('shadowed_by_blocks', 0)} shadowed").font = normal_font
                    row += 1

        audit_widths = [28, 14, 12, 12, 14, 14, 14, 12]
        for i, w in enumerate(audit_widths):
            ws4.column_dimensions[get_column_letter(i + 1)].width = w

    # Save to bytes
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


# ─── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TraceQ — BOQ Risk Review",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Branding & Styles ───────────────────────────────────────────────────────
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1a1a2e;
        margin-bottom: 0;
    }
    .sub-header {
        font-size: 1.1rem;
        color: #666;
        margin-top: -10px;
        margin-bottom: 30px;
    }
    .metric-card {
        background: #f8f9fa;
        border-radius: 12px;
        padding: 20px;
        border-left: 4px solid #0066cc;
    }
    .risk-high { color: #dc3545; font-weight: 700; }
    .risk-medium { color: #fd7e14; font-weight: 700; }
    .risk-low { color: #28a745; font-weight: 700; }
    .risk-verify { color: #6f42c1; font-weight: 700; }
    .stMetric > div { background: #f8f9fa; border-radius: 10px; padding: 10px; }
</style>
""", unsafe_allow_html=True)

# ─── Header ───────────────────────────────────────────────────────────────────
st.markdown('<p class="main-header">🔍 TraceQ</p>', unsafe_allow_html=True)
st.markdown('<p class="sub-header">BOQ Risk Review Engine — by TechTelligence</p>', unsafe_allow_html=True)

# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Upload Files")
    st.markdown("Upload your HVAC drawing(s) to analyse.")

    drawing_files = st.file_uploader(
        "📐 Drawing File(s) (DXF or DWG)",
        type=["dxf", "dwg"],
        accept_multiple_files=True,
        help="Upload one or more HVAC layout drawings in DXF or DWG format."
    )

    boq_file = st.file_uploader(
        "📊 BOQ Spreadsheet (optional)",
        type=["xlsx", "xls", "csv"],
        help="Upload the Bill of Quantities for comparison. If not provided, TraceQ will still count all equipment found in the drawing."
    )

    st.markdown("---")
    st.markdown("### About")
    st.markdown(
        "TraceQ analyses HVAC drawings and compares equipment counts "
        "against the BOQ to identify discrepancies and missing items."
    )
    st.markdown(
        "**Three-tier detection:**\n"
        "1. Layer-based classification\n"
        "2. Block name matching\n"
        "3. Text label analysis"
    )
    st.markdown("---")
    st.markdown("### DWG Support")
    st.markdown(
        "✅ **DWG files are supported.** Upload a DWG directly and "
        "TraceQ will convert it to DXF automatically on the server."
    )
    st.markdown(
        "_If auto-conversion fails, you can also convert manually:_\n"
        "- **AutoCAD/BricsCAD**: File → Save As → DXF\n"
        "- **Online**: [CloudConvert](https://cloudconvert.com/dwg-to-dxf)"
    )
    st.markdown("---")
    st.markdown("*Built by [TechTelligence](mailto:nicholas@ttelligence.com)*")
    st.markdown("*v1.2 — March 2026*")


# ─── Main Content ─────────────────────────────────────────────────────────────

if not drawing_files:
    # Landing state — no file uploaded yet
    st.markdown("---")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("#### 📐 Upload Drawing(s)")
        st.markdown("Upload your HVAC layout drawing(s) (DXF or DWG) using the sidebar.")
    with col2:
        st.markdown("#### 🔍 Automatic Analysis")
        st.markdown("TraceQ scans every layer, block, and text label to count equipment.")
    with col3:
        st.markdown("#### 📊 Get Your Report")
        st.markdown("See discrepancies, missing items, and cost exposure at a glance.")

    st.markdown("---")
    st.info("👈 Upload one or more DXF/DWG files in the sidebar to get started.")

else:
    # ─── Prepare all uploaded drawing files ───────────────────────────────────
    tmp_paths = []  # List of (filename, tmp_path) tuples
    for drawing_file in drawing_files:
        file_ext = os.path.splitext(drawing_file.name)[1].lower() or '.dxf'
        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
            tmp.write(drawing_file.read())
            tmp_path = tmp.name

        # DWG → DXF Auto-Conversion
        if file_ext == '.dwg':
            with st.spinner(f"Converting {drawing_file.name} DWG to DXF..."):
                try:
                    dxf_path = FileConverter.convert_dwg_to_dxf(tmp_path)
                    tmp_path = dxf_path
                    st.success(f"✅ Converted **{drawing_file.name}** to DXF successfully.")
                except RuntimeError as e:
                    st.error(
                        f"⚠️ Could not convert {drawing_file.name} automatically.\n\n"
                        f"**What to do:** Open the DWG in AutoCAD or BricsCAD → File → Save As → DXF, "
                        f"then upload the DXF version.\n\n"
                        f"_Technical detail: {str(e)}_"
                    )
                    continue  # Skip this file, process the rest

        tmp_paths.append((drawing_file.name, tmp_path))

    if not tmp_paths:
        st.error("No valid drawing files to process.")
        st.stop()

    # ─── Display file count ───────────────────────────────────────────────────
    drawing_names = [name for name, _ in tmp_paths]
    drawing_name_combined = " + ".join(drawing_names)
    if len(tmp_paths) > 1:
        st.info(f"📂 **{len(tmp_paths)} drawing files** uploaded for combined analysis.")

    # ─── Run Quick Scan on first file (shared between tabs) ───────────────────
    # For multi-file: scan the first file for Quick Scan tab, full analysis merges all
    scan = None
    with st.spinner("Running quick scan..."):
        try:
            engine = TraceQEngine()
            scan = engine.quick_scan(tmp_paths[0][1])
        except Exception as e:
            st.error(f"Quick scan failed: {str(e)}")

    # ─── Step 0: Quick Scan + Full Analysis Tabs ──────────────────────────────
    tab_scan, tab_analysis = st.tabs(["Step 0: Quick Scan", "Full Analysis"])

    # ═══ TAB 1: QUICK SCAN ═══════════════════════════════════════════════════
    with tab_scan:
        st.markdown("### Step 0 — Compatibility Scan")
        st.caption("Quick check: how much of this drawing does TraceQ recognise?")

        if scan and scan._dwg_unsupported:
            st.error(scan.verdict_msg)
        elif scan:
            # ── Overall Score ──
            if scan.verdict == 'HIGH':
                score_color = "🟢"
                st.success(f"{score_color} **Overall Compatibility: {scan.overall_score}% — HIGH**")
                st.info(scan.verdict_msg)
            elif scan.verdict == 'MEDIUM':
                score_color = "🟡"
                st.warning(f"{score_color} **Overall Compatibility: {scan.overall_score}% — MEDIUM**")
                st.info(scan.verdict_msg)
            else:
                score_color = "🔴"
                st.error(f"{score_color} **Overall Compatibility: {scan.overall_score}% — LOW**")
                st.info(scan.verdict_msg)

            # ── Score Breakdown ──
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Layers", f"{scan.layer_score}%",
                          delta=f"{len(scan.recognised_layers)}/{scan.hvac_candidate_layers}")
            with col2:
                st.metric("Blocks", f"{scan.block_score}%",
                          delta=f"{len(scan.recognised_blocks)}/{scan.total_blocks}")
            with col3:
                st.metric("Text Patterns", f"{scan.mtext_score}%",
                          delta=f"{scan.mtext_pattern_hits}/{scan.total_mtext_patterns}")
            with col4:
                st.metric("Total Entities", f"{scan.total_entities:,}")

            st.markdown("---")

            # ── Recognised Layers ──
            if scan.recognised_layers:
                with st.expander(f"✅ Recognised Layers ({len(scan.recognised_layers)})", expanded=True):
                    layer_data = []
                    for rl in scan.recognised_layers:
                        layer_data.append({
                            "Layer Name": rl['layer'],
                            "Equipment Type": rl['equipment_type'].replace('_', ' ').title(),
                            "Confidence": f"{rl['confidence']:.0%}",
                            "Match": rl['method'],
                        })
                    st.dataframe(layer_data, use_container_width=True, hide_index=True)

            # ── Unrecognised Layers ──
            if scan.unrecognised_layers:
                with st.expander(f"❓ Unrecognised Layers ({len(scan.unrecognised_layers)})", expanded=False):
                    st.caption("These layers may contain equipment that TraceQ doesn't recognise yet. Nestor can help identify them.")
                    for ul in scan.unrecognised_layers:
                        st.text(f"  {ul}")

            # ── Recognised Blocks ──
            if scan.recognised_blocks:
                with st.expander(f"✅ Recognised Blocks ({len(scan.recognised_blocks)})", expanded=True):
                    block_data = []
                    for rb in scan.recognised_blocks:
                        block_data.append({
                            "Block Name": rb['block'],
                            "Equipment Type": rb['equipment_type'].replace('_', ' ').title(),
                            "Count": rb['count'],
                            "Match": rb['match'],
                        })
                    st.dataframe(block_data, use_container_width=True, hide_index=True)

            # ── Unrecognised Blocks ──
            if scan.unrecognised_blocks:
                with st.expander(f"❓ Unrecognised Blocks ({len(scan.unrecognised_blocks)})", expanded=False):
                    st.caption("These blocks may be equipment. Nestor can identify them to expand the dictionary.")
                    block_unk = []
                    for ub in scan.unrecognised_blocks:
                        block_unk.append({
                            "Block Name": ub['block'],
                            "Occurrences": ub['count'],
                        })
                    st.dataframe(block_unk, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.caption("Tip: After running the full analysis, send unrecognised items to Nestor for identification. His corrections will permanently improve TraceQ's accuracy.")

    # ═══ TAB 2: FULL ANALYSIS ════════════════════════════════════════════════
    with tab_analysis:
        # ─── Analyse all drawing files and merge results ─────────────────────────
        all_results = []
        combined_merged = {}
        combined_parse_info = {'layers': 0, 'block_types': 0}
        combined_dedup_report = None

        with st.spinner(f"Analysing {'drawings' if len(tmp_paths) > 1 else 'drawing'}... this may take a moment."):
            engine = TraceQEngine()
            for fname, fpath in tmp_paths:
                try:
                    result = engine.analyze(fpath)
                    all_results.append((fname, result))

                    # Merge equipment counts across files
                    file_merged = result.detection_results.get('merged', {})
                    for equip_type, data in file_merged.items():
                        if equip_type not in combined_merged:
                            combined_merged[equip_type] = {
                                'count': data.get('count', 0),
                                'source': data.get('source', 'unknown'),
                                'confidence': data.get('confidence', 0),
                                'items': list(data.get('items', [])),
                                'alternate_counts': dict(data.get('alternate_counts', {})),
                                'needs_review': data.get('needs_review', False),
                            }
                            if data.get('notes'):
                                combined_merged[equip_type]['notes'] = data['notes']
                        else:
                            # Sum counts from additional files
                            existing = combined_merged[equip_type]
                            existing['count'] += data.get('count', 0)
                            existing['items'] = existing.get('items', []) + list(data.get('items', []))
                            # Sum alternate counts
                            for tier_key in ['tier1', 'tier2', 'tier3']:
                                existing['alternate_counts'][tier_key] = (
                                    existing['alternate_counts'].get(tier_key, 0)
                                    + data.get('alternate_counts', {}).get(tier_key, 0)
                                )
                            # Keep needs_review if any file flags it
                            if data.get('needs_review'):
                                existing['needs_review'] = True

                    # Merge parse info
                    p = result.parse_info
                    combined_parse_info['layers'] += p.get('layers', 0)
                    combined_parse_info['block_types'] += p.get('block_types', 0)

                except Exception as e:
                    st.error(f"Analysis failed for {fname}: {str(e)}")

            # Use the last result's dedup report (typically only one file has it)
            if all_results:
                result = all_results[-1][1]  # Keep last result for dedup/validation
                combined_dedup_report = getattr(result, 'dedup_report', None)

        # Clean up temp files
        for _, fpath in tmp_paths:
            try:
                os.unlink(fpath)
            except OSError:
                pass

        if not all_results:
            st.error("No drawing files could be analysed.")
            st.stop()

        # ─── Results Header ───────────────────────────────────────────────────────
        if len(all_results) == 1:
            st.success(f"✅ Analysis complete — **{all_results[0][0]}**")
        else:
            st.success(f"✅ Analysis complete — **{len(all_results)} files** combined")
        st.markdown("---")

        # ─── Key Metrics ──────────────────────────────────────────────────────────
        merged = combined_merged
        total_items = sum(v.get('count', 0) for v in merged.values())
        total_categories = len(merged)
        parser_info = combined_parse_info
        _dname = drawing_names[0].split('.')[0] if len(drawing_names) == 1 else f"{len(drawing_names)}_files"

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Equipment", f"{total_items:,}")
        with col2:
            st.metric("Categories Found", total_categories)
        with col3:
            st.metric("Layers Scanned", parser_info.get('layers', 0))
        with col4:
            st.metric("Block Types", parser_info.get('block_types', 0))

        st.markdown("---")

        # ─── Equipment Inventory ──────────────────────────────────────────────────
        st.markdown("### 📋 Equipment Inventory")

        table_data = []
        review_items = []
        for equip_type, data in sorted(merged.items()):
            count = data.get('count', 0)
            source = data.get('source', 'unknown')
            confidence = data.get('confidence', 0)
            alt = data.get('alternate_counts', {})
            flagged = data.get('needs_review', False)

            if 'tier1' in source:
                source_label = "🟢 Layer"
            elif 'tier2' in source:
                source_label = "🔵 Block"
            elif 'tier3' in source:
                source_label = "🟡 Text"
            else:
                source_label = f"⚪ {source}"

            name = _format_equipment_name(equip_type)

            # Show all three tier counts explicitly
            t1 = alt.get('tier1', 0)
            t2 = alt.get('tier2', 0)
            t3 = alt.get('tier3', 0)

            row = {
                "Equipment": name,
                "Count": count,
                "Source": source_label,
                "Confidence": f"{int(confidence * 100)}%",
                "Layer": t1 if t1 > 0 else "—",
                "Block": t2 if t2 > 0 else "—",
                "Text": t3 if t3 > 0 else "—",
            }

            if flagged:
                row["Flag"] = "⚠️ Review"
                review_items.append({
                    'name': name,
                    'note': data.get('notes', 'Tier counts disagree significantly.'),
                    'tier1': t1, 'tier2': t2, 'tier3': t3,
                })
            else:
                row["Flag"] = "✅"

            table_data.append(row)

        if table_data:
            st.dataframe(
                table_data,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Equipment": st.column_config.TextColumn("Equipment", width="medium"),
                    "Count": st.column_config.NumberColumn("Count", width="small"),
                    "Source": st.column_config.TextColumn("Source", width="small"),
                    "Confidence": st.column_config.TextColumn("Conf.", width="small"),
                    "Layer": st.column_config.TextColumn("Layer (T1)", width="small"),
                    "Block": st.column_config.TextColumn("Block (T2)", width="small"),
                    "Text": st.column_config.TextColumn("Text (T3)", width="small"),
                    "Flag": st.column_config.TextColumn("Status", width="small"),
                }
            )

        # Show review warnings if any
        if review_items:
            st.markdown("#### ⚠️ Items Flagged for QS Review")
            for item in review_items:
                st.warning(
                    f"**{item['name']}** — Tier counts disagree: "
                    f"Layer={item['tier1']}, Block={item['tier2']}, Text={item['tier3']}. "
                    f"Recommend manual verification."
                )

        # Show dedup report if any proximity deductions were made
        dedup_report = result.detection_results.get('dedup_report', {})
        if dedup_report:
            adjustments = dedup_report.get('adjustments', [])
            if adjustments:
                with st.expander(f"🔗 Proximity Deduplication ({len(adjustments)} adjustments)", expanded=False):
                    st.caption(
                        "Text labels found near block INSERTs of the same equipment type — "
                        "Tier 3 count reduced to avoid double-counting."
                    )
                    for adj in adjustments:
                        st.info(
                            f"**{_format_equipment_name(adj.get('equipment_type', ''))}** — "
                            f"Tier 3 reduced from {adj.get('tier3_original', 0)} to {adj.get('tier3_adjusted', 0)} "
                            f"({adj.get('shadowed_by_blocks', 0)} text labels near blocks, "
                            f"radius: {dedup_report.get('radius_used', 0):.0f} units)"
                        )

        st.markdown("---")

        # ─── BOQ Comparison (if BOQ uploaded) ─────────────────────────────────────
        if boq_file is not None:
            st.markdown("### 📊 BOQ Discrepancy Report")

            try:
                boq_bytes = boq_file.read()
                boq_items = parse_boq(boq_bytes, boq_file.name)

                if boq_items:
                    comparisons, missing_from_boq = compare_boq_vs_drawing(boq_items, merged)

                    # ── Summary Metrics ──
                    matches = sum(1 for c in comparisons if c['Risk'] == 'MATCH')
                    discrepancies = sum(1 for c in comparisons if c['Risk'] in ('HIGH', 'MEDIUM', 'LOW'))
                    verify_items = sum(1 for c in comparisons if c['Risk'] == 'VERIFY')
                    missing_count = len(missing_from_boq)
                    total_exposure = sum(c.get('_exposure_num') or 0 for c in comparisons)

                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric("Matching", matches)
                    with col2:
                        st.metric("Discrepancies", discrepancies)
                    with col3:
                        st.metric("Needs Verification", verify_items)
                    with col4:
                        st.metric("Total Exposure", f"AED {total_exposure:,.0f}")

                    # ── EXCEL DOWNLOAD — top of report ──
                    excel_bytes = generate_excel_report(
                        comparisons, missing_from_boq, boq_items,
                        drawing_name_combined, boq_file.name,
                        merged=merged,
                        dedup_report=result.detection_results.get('dedup_report'),
                    )
                    report_filename = f"TraceQ_BOQ_Report_{_dname}_{datetime.now().strftime('%Y%m%d')}.xlsx"

                    col_dl1, col_dl2 = st.columns(2)
                    with col_dl1:
                        st.download_button(
                            label="📥 Download BOQ Report (Client)",
                            data=excel_bytes,
                            file_name=report_filename,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            type="primary",
                        )
                    with col_dl2:
                        nestor_bytes = generate_nestor_feedback(
                            comparisons, missing_from_boq, merged,
                            drawing_name_combined,
                            scan=scan,
                        )
                        nestor_filename = f"TraceQ_QS_Feedback_{_dname}_{datetime.now().strftime('%Y%m%d')}.xlsx"
                        st.download_button(
                            label="📋 Download QS Feedback Sheet",
                            data=nestor_bytes,
                            file_name=nestor_filename,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )

                    st.markdown("---")

                    # ── Main Comparison Table ──
                    st.markdown("#### Comparison Details")

                    display_comparisons = []
                    for c in comparisons:
                        display_comparisons.append({
                            'Trace ID': c['Trace ID'],
                            'Equipment': c['Equipment'],
                            'BOQ Qty': c['BOQ Qty'],
                            'Drawing Qty': c['Drawing Qty'],
                            'Diff': c['Difference'],
                            'Unit': c['Unit'],
                            'Risk': c['Risk'],
                            'Exposure (AED)': c['Exposure (AED)'],
                            'Notes': c['Notes'],
                        })

                    st.dataframe(
                        display_comparisons,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Trace ID": st.column_config.TextColumn("Trace ID", width="small"),
                            "Equipment": st.column_config.TextColumn("Equipment", width="medium"),
                            "BOQ Qty": st.column_config.TextColumn("BOQ", width="small"),
                            "Drawing Qty": st.column_config.TextColumn("Drawing", width="small"),
                            "Diff": st.column_config.TextColumn("Diff", width="small"),
                            "Unit": st.column_config.TextColumn("Unit", width="small"),
                            "Risk": st.column_config.TextColumn("Risk", width="small"),
                            "Exposure (AED)": st.column_config.TextColumn("Exposure", width="small"),
                            "Notes": st.column_config.TextColumn("Notes", width="large"),
                        }
                    )

                    # ── Missing from BOQ ──
                    if missing_from_boq:
                        st.markdown(f"#### Items in Drawing Not in BOQ ({missing_count} items)")
                        st.caption("These items were detected in the drawing but have no corresponding BOQ line item.")

                        missing_display = []
                        for m in missing_from_boq:
                            missing_display.append({
                                'Trace ID': m['Trace ID'],
                                'Equipment': m['Equipment'],
                                'Drawing Qty': m['Drawing Qty'],
                                'Detection': m['Detection'],
                                'Confidence': m['Confidence'],
                                'Notes': m['Notes'],
                            })

                        st.dataframe(
                            missing_display,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "Trace ID": st.column_config.TextColumn("Trace ID", width="small"),
                                "Equipment": st.column_config.TextColumn("Equipment", width="medium"),
                                "Drawing Qty": st.column_config.NumberColumn("Qty", width="small"),
                                "Detection": st.column_config.TextColumn("Detection", width="medium"),
                                "Confidence": st.column_config.TextColumn("Confidence", width="small"),
                                "Notes": st.column_config.TextColumn("Notes", width="large"),
                            }
                        )

                    # ── Parsed BOQ Line Items (detail expander) ──
                    with st.expander("📄 Parsed BOQ Line Items", expanded=False):
                        boq_display = []
                        for item in boq_items:
                            boq_display.append({
                                "Ref": item.get('boq_ref', '—'),
                                "Description": item['description'][:70],
                                "Type": (item['equipment_type'] or '—').replace('_', ' ').title(),
                                "Unit": item.get('unit', '—'),
                                "Qty": int(item['qty']) if item['qty'] == int(item['qty']) else item['qty'],
                                "Rate": f"{item['rate']:,.0f}" if item.get('rate') else '—',
                                "Total": f"{item['total']:,.0f}" if item.get('total') else '—',
                            })
                        st.dataframe(boq_display, use_container_width=True, hide_index=True)
                else:
                    st.warning("Could not parse any equipment items from the BOQ file. Check the format.")

            except Exception as e:
                st.error(f"Error reading BOQ file: {str(e)}")

            st.markdown("---")

        # ─── Validation Results ───────────────────────────────────────────────────
        st.markdown("### ⚠️ Validation Checks")

        validation = result.validation_results
        warnings = validation.get('warnings', [])

        if not warnings:
            st.success("All validation checks passed — no warnings.")
        else:
            for w in warnings:
                if isinstance(w, dict):
                    severity = w.get('severity', 'info')
                    msg = w.get('message', str(w))
                    if severity == 'warning':
                        st.warning(msg)
                    elif severity == 'critical':
                        st.error(msg)
                    else:
                        st.info(msg)
                else:
                    w_str = str(w)
                    if w_str.startswith('[WARNING]'):
                        st.warning(w_str)
                    elif w_str.startswith('[CRITICAL]'):
                        st.error(w_str)
                    else:
                        st.info(w_str)

        st.markdown("---")

        # ─── Layer Classification ─────────────────────────────────────────────────
        with st.expander("🗂️ Layer Classification Details", expanded=False):
            layer_results = result.layer_classification
            classified = []
            unclassified = []

            for layer_name, info in sorted(layer_results.items()):
                equip = info.get('equipment_type')
                conf = info.get('confidence', 0)
                method = info.get('method', 'unknown')

                if equip:
                    classified.append({
                        "Layer": layer_name,
                        "Equipment Type": equip.replace('_', ' ').title(),
                        "Confidence": f"{int(conf * 100)}%",
                        "Match": method,
                    })
                else:
                    unclassified.append(layer_name)

            if classified:
                st.markdown("**Classified Layers:**")
                st.dataframe(classified, use_container_width=True, hide_index=True)

            if unclassified:
                st.markdown(f"**Unclassified Layers ({len(unclassified)}):**")
                st.text(", ".join(unclassified))

        # ─── Detection Tier Breakdown ─────────────────────────────────────────────
        with st.expander("📊 Detection Tier Breakdown", expanded=False):
            for tier_name in ['tier1', 'tier2', 'tier3']:
                tier_data = result.detection_results.get(tier_name, {})
                if tier_data:
                    labels = {'tier1': '🟢 Tier 1 — Layer Detection',
                              'tier2': '🔵 Tier 2 — Block Detection',
                              'tier3': '🟡 Tier 3 — Text Detection'}
                    st.markdown(f"**{labels[tier_name]}**")
                    tier_items = []
                    for equip, data in sorted(tier_data.items()):
                        tier_items.append({
                            "Equipment": equip.replace('_', ' ').title(),
                            "Count": data.get('count', 0),
                        })
                    st.dataframe(tier_items, use_container_width=True, hide_index=True)

        # ─── Raw JSON Output ──────────────────────────────────────────────────────
        with st.expander("🔧 Raw JSON Output", expanded=False):
            st.json(result.to_dict())

        # ─── Download Button (JSON fallback — always available) ───────────────────
        st.markdown("---")
        json_output = json.dumps(result.to_dict(), indent=2)
        st.download_button(
            label="📥 Download Full Analysis (JSON)",
            data=json_output,
            file_name=f"TraceQ_Analysis_{_dname}_{datetime.now().strftime('%Y%m%d')}.json",
            mime="application/json",
        )
