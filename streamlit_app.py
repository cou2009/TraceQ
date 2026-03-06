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
from traceq_engine import TraceQEngine, Config


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
        name = etype.replace('_', ' ').title()

        # For VERIFY items with non-countable units, don't show misleading diff/exposure
        if risk == 'VERIFY' and has_non_countable:
            show_diff = '—'
            show_exposure = '—'
        else:
            show_diff = f"{int(diff):+d}" if drawing_qty > 0 else '—'
            show_exposure = f"{exposure:,.0f}" if exposure > 0 else '—'

        # Assign trace ID
        trace_counter += 1
        trace_id = f"TQ-{trace_counter:03d}"

        comparisons.append({
            'Trace ID': trace_id,
            'Equipment': name,
            'BOQ Qty': int(boq_qty) if boq_qty == int(boq_qty) else f"{boq_qty:,.1f}",
            'Drawing Qty': int(drawing_qty) if drawing_qty else '—',
            'Difference': show_diff,
            'Unit': ', '.join(sorted(units)) if units else '—',
            'Risk': risk,
            'Exposure (AED)': show_exposure,
            'Notes': note,
            'BOQ Breakdown': boq_breakdown,
            'Detection Source': source,
            '_exposure_num': exposure if risk != 'VERIFY' else 0,
            '_boq_qty': boq_qty,
            '_drawing_qty': drawing_qty,
            '_diff': diff,
            '_rate': avg_rate,
        })

    # Missing from BOQ: items in drawing but not in any BOQ type
    missing_from_boq = []
    for etype, data in sorted(drawing_merged.items()):
        if etype not in matched_drawing_types and data.get('count', 0) > 0:
            trace_counter += 1
            trace_id = f"TQ-{trace_counter:03d}"
            name = etype.replace('_', ' ').title()
            source = data.get('source', 'unknown')
            confidence = data.get('confidence', 0)
            missing_from_boq.append({
                'Trace ID': trace_id,
                'Equipment': name,
                'Drawing Qty': data['count'],
                'Detection': source,
                'Confidence': f"{int(confidence * 100)}%",
                'Notes': f"Found in drawing ({source}) but no matching BOQ line item.",
            })

    return comparisons, missing_from_boq


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

def generate_excel_report(comparisons, missing_from_boq, boq_items, drawing_name, boq_name):
    """
    Generate a professional Excel BOQ Discrepancy Report with 3 tabs:
      Tab 1: Executive Summary
      Tab 2: Discrepancy Details
      Tab 3: Items Not in BOQ
    Returns bytes of the .xlsx file.
    """
    wb = openpyxl.Workbook()

    # Styles
    header_font = Font(name='Arial', bold=True, size=11, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1A1A2E')
    title_font = Font(name='Arial', bold=True, size=14, color='1A1A2E')
    subtitle_font = Font(name='Arial', bold=False, size=10, color='666666')
    bold_font = Font(name='Arial', bold=True, size=10)
    normal_font = Font(name='Arial', size=10)
    number_font = Font(name='Arial', size=10)
    match_fill = PatternFill('solid', fgColor='D4EDDA')
    high_fill = PatternFill('solid', fgColor='F8D7DA')
    medium_fill = PatternFill('solid', fgColor='FFF3CD')
    low_fill = PatternFill('solid', fgColor='D1ECF1')
    verify_fill = PatternFill('solid', fgColor='E8DAEF')
    missing_fill = PatternFill('solid', fgColor='FADBD8')
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

    # Title block
    ws1['A1'] = 'TraceQ — BOQ Discrepancy Report'
    ws1['A1'].font = title_font
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
    total_exposure = sum(c.get('_exposure_num', 0) for c in comparisons)

    row = 7
    ws1.cell(row=row, column=1, value='SUMMARY').font = bold_font
    row += 1
    labels = [
        ('Items Matching', matches),
        ('Discrepancies Found', discrepancies),
        ('Items Needing Verification', verify_count),
        ('Items Missing from BOQ', missing_count),
        ('Total Quantifiable Exposure (AED)', total_exposure),
    ]
    for label, val in labels:
        ws1.cell(row=row, column=1, value=label).font = normal_font
        c = ws1.cell(row=row, column=2, value=val)
        c.font = bold_font
        if isinstance(val, (int, float)) and label.startswith('Total'):
            c.number_format = '#,##0'
        row += 1

    # Risk breakdown table
    row += 1
    ws1.cell(row=row, column=1, value='RISK BREAKDOWN').font = bold_font
    row += 1
    risk_headers = ['Trace ID', 'Equipment', 'Risk', 'BOQ Qty', 'Drawing Qty', 'Exposure (AED)']
    for col_idx, h in enumerate(risk_headers, 1):
        c = ws1.cell(row=row, column=col_idx, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center')
        c.border = thin_border
    row += 1

    # Only show non-MATCH items in executive summary
    for comp in comparisons:
        if comp['Risk'] == 'MATCH':
            continue
        vals = [
            comp['Trace ID'],
            comp['Equipment'],
            comp['Risk'],
            comp.get('_boq_qty', 0),
            comp.get('_drawing_qty', 0),
            comp.get('_exposure_num', 0),
        ]
        fill = risk_fills.get(comp['Risk'], None)
        for col_idx, v in enumerate(vals, 1):
            c = ws1.cell(row=row, column=col_idx, value=v)
            c.font = normal_font
            c.border = thin_border
            if fill:
                c.fill = fill
            if col_idx in (4, 5):
                c.number_format = '#,##0'
            if col_idx == 6:
                c.number_format = '#,##0'
            c.alignment = Alignment(horizontal='center') if col_idx != 2 else Alignment(horizontal='left')
        row += 1

    # Missing items in exec summary
    if missing_from_boq:
        for m in missing_from_boq:
            vals = [
                m['Trace ID'],
                m['Equipment'],
                'MISSING FROM BOQ',
                '—',
                m['Drawing Qty'],
                '—',
            ]
            for col_idx, v in enumerate(vals, 1):
                c = ws1.cell(row=row, column=col_idx, value=v)
                c.font = normal_font
                c.border = thin_border
                c.fill = missing_fill
                c.alignment = Alignment(horizontal='center') if col_idx != 2 else Alignment(horizontal='left')
            row += 1

    # Column widths
    ws1.column_dimensions['A'].width = 35
    ws1.column_dimensions['B'].width = 25
    ws1.column_dimensions['C'].width = 18
    ws1.column_dimensions['D'].width = 12
    ws1.column_dimensions['E'].width = 14
    ws1.column_dimensions['F'].width = 16

    # ─── Tab 2: Discrepancy Details ───────────────────────────────────────────
    ws2 = wb.create_sheet("Discrepancy Details")
    ws2.sheet_properties.tabColor = "DC3545"

    ws2['A1'] = 'BOQ vs Drawing — Detailed Comparison'
    ws2['A1'].font = title_font
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
        vals = [
            comp['Trace ID'],
            comp['Equipment'],
            comp.get('_boq_qty', 0),
            comp.get('_drawing_qty', 0),
            comp.get('_diff', 0),
            comp['Unit'],
            comp['Risk'],
            comp.get('_exposure_num', 0),
            comp.get('Detection Source', '—'),
            comp['Notes'],
            comp.get('BOQ Breakdown', ''),
        ]
        fill = risk_fills.get(comp['Risk'], None)
        for col_idx, v in enumerate(vals, 1):
            c = ws2.cell(row=row, column=col_idx, value=v)
            c.font = normal_font
            c.border = thin_border
            if fill and col_idx == 7:
                c.fill = fill
            if col_idx in (3, 4, 5):
                c.number_format = '#,##0'
            if col_idx == 8:
                c.number_format = '#,##0'
            if col_idx in (10, 11):
                c.alignment = Alignment(wrap_text=True)
            elif col_idx in (1, 3, 4, 5, 6, 7, 8):
                c.alignment = Alignment(horizontal='center')
        row += 1

    # Column widths for details tab
    detail_widths = [12, 25, 10, 12, 10, 8, 12, 14, 22, 55, 45]
    for i, w in enumerate(detail_widths):
        ws2.column_dimensions[get_column_letter(i + 1)].width = w

    # ─── Tab 3: Items Not in BOQ ──────────────────────────────────────────────
    ws3 = wb.create_sheet("Not in BOQ")
    ws3.sheet_properties.tabColor = "FD7E14"

    ws3['A1'] = 'Items Detected in Drawing — Not in BOQ'
    ws3['A1'].font = title_font
    ws3['A2'] = 'These items were found in the drawing but have no corresponding BOQ line item. They may represent scope gaps or items not yet priced.'
    ws3['A2'].font = subtitle_font

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
            vals = [
                m['Trace ID'],
                m['Equipment'],
                m['Drawing Qty'],
                m['Detection'],
                m['Confidence'],
                m['Notes'],
            ]
            for col_idx, v in enumerate(vals, 1):
                c = ws3.cell(row=row, column=col_idx, value=v)
                c.font = normal_font
                c.border = thin_border
                c.fill = missing_fill
                if col_idx == 6:
                    c.alignment = Alignment(wrap_text=True)
                else:
                    c.alignment = Alignment(horizontal='center') if col_idx != 2 else Alignment(horizontal='left')
            row += 1
    else:
        ws3.cell(row=row, column=1, value='No items missing from BOQ.').font = normal_font

    # Column widths
    not_in_boq_widths = [12, 25, 14, 22, 12, 55]
    for i, w in enumerate(not_in_boq_widths):
        ws3.column_dimensions[get_column_letter(i + 1)].width = w

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
    st.markdown("Upload your HVAC drawing to analyse.")

    drawing_file = st.file_uploader(
        "📐 Drawing File (DXF or DWG)",
        type=["dxf", "dwg"],
        help="Upload the HVAC layout drawing in DXF or DWG format."
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
    st.markdown("*Built by [TechTelligence](mailto:nicholas@ttelligence.com)*")
    st.markdown("*v1.0 — March 2026*")


# ─── Main Content ─────────────────────────────────────────────────────────────

if drawing_file is None:
    # Landing state — no file uploaded yet
    st.markdown("---")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("#### 📐 Upload a Drawing")
        st.markdown("Upload your HVAC layout drawing (DXF or DWG) using the sidebar.")
    with col2:
        st.markdown("#### 🔍 Automatic Analysis")
        st.markdown("TraceQ scans every layer, block, and text label to count equipment.")
    with col3:
        st.markdown("#### 📊 Get Your Report")
        st.markdown("See discrepancies, missing items, and cost exposure at a glance.")

    st.markdown("---")
    st.info("👈 Upload a DXF or DWG file in the sidebar to get started.")

else:
    # ─── Run Analysis ─────────────────────────────────────────────────────────
    with st.spinner("Analysing drawing... this may take a moment."):
        file_ext = os.path.splitext(drawing_file.name)[1].lower() or '.dxf'
        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
            tmp.write(drawing_file.read())
            tmp_path = tmp.name

        try:
            engine = TraceQEngine()
            result = engine.analyze(tmp_path)
            os.unlink(tmp_path)
        except Exception as e:
            os.unlink(tmp_path)
            st.error(f"Analysis failed: {str(e)}")
            st.stop()

    # ─── Results Header ───────────────────────────────────────────────────────
    st.success(f"✅ Analysis complete — **{drawing_file.name}**")
    st.markdown("---")

    # ─── Key Metrics ──────────────────────────────────────────────────────────
    merged = result.detection_results.get('merged', {})
    total_items = sum(v.get('count', 0) for v in merged.values())
    total_categories = len(merged)
    parser_info = result.parse_info

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
    for equip_type, data in sorted(merged.items()):
        count = data.get('count', 0)
        source = data.get('source', 'unknown')
        confidence = data.get('confidence', 0)
        alt = data.get('alternate_counts', {})

        if 'tier1' in source:
            source_label = "Layer"
            tier_icon = "🟢"
        elif 'tier2' in source:
            source_label = "Block"
            tier_icon = "🔵"
        elif 'tier3' in source:
            source_label = "Text"
            tier_icon = "🟡"
        else:
            source_label = source
            tier_icon = "⚪"

        name = equip_type.replace('_', ' ').title()

        alts = []
        for k, v in alt.items():
            if v > 0 and v != count:
                alts.append(f"{k}: {v}")
        alt_str = ", ".join(alts) if alts else "—"

        table_data.append({
            "Equipment": name,
            "Count": count,
            "Detection": f"{tier_icon} {source_label}",
            "Confidence": f"{int(confidence * 100)}%",
            "Other Counts": alt_str,
        })

    if table_data:
        st.dataframe(
            table_data,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Equipment": st.column_config.TextColumn("Equipment", width="medium"),
                "Count": st.column_config.NumberColumn("Count", width="small"),
                "Detection": st.column_config.TextColumn("Detection Method", width="small"),
                "Confidence": st.column_config.TextColumn("Confidence", width="small"),
                "Other Counts": st.column_config.TextColumn("Alternate Counts", width="medium"),
            }
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
                total_exposure = sum(c.get('_exposure_num', 0) for c in comparisons)

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
                    drawing_file.name, boq_file.name,
                )
                report_filename = f"TraceQ_BOQ_Report_{drawing_file.name.split('.')[0]}_{datetime.now().strftime('%Y%m%d')}.xlsx"

                st.download_button(
                    label="📥 Download BOQ Discrepancy Report (Excel)",
                    data=excel_bytes,
                    file_name=report_filename,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
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
        file_name=f"TraceQ_Analysis_{drawing_file.name}_{datetime.now().strftime('%Y%m%d')}.json",
        mime="application/json",
    )
