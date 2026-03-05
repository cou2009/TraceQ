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
import tempfile
from datetime import datetime

import openpyxl

# Import the TraceQ engine (same directory)
from traceq_engine import TraceQEngine, Config


# ─── BOQ Parser ───────────────────────────────────────────────────────────────

# Mapping: keywords in BOQ descriptions → engine equipment types
BOQ_KEYWORD_MAP = [
    # Order matters — more specific matches first
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


def parse_boq(file_bytes, filename):
    """
    Parse a BOQ Excel file and extract equipment items.
    Returns list of dicts: [{description, equipment_type, qty, unit, rate, total}]
    """
    # Save to temp file for openpyxl
    suffix = os.path.splitext(filename)[1].lower() or '.xlsx'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        wb = openpyxl.load_workbook(tmp_path, data_only=True)
        ws = wb.active

        items = []
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True):
            # Find rows that have a description and a numeric quantity
            # BOQ format: col B = item#/desc, col C = desc, col D = unit, col E = qty, col F = rate, col G = total
            # But format varies — scan all columns for description + qty
            desc = None
            qty = None
            unit = None
            rate = None
            total = None

            for i, val in enumerate(row):
                if val is None:
                    continue
                s = str(val).strip()
                if not s:
                    continue

                # Look for quantity (numeric, reasonable range)
                if qty is None and isinstance(val, (int, float)) and 0 < val < 100000:
                    # Check if this is likely a qty (not a rate or item number)
                    # Heuristic: if we already found a description, this could be qty
                    if desc and i > 1:
                        qty = val
                        # Try to get unit, rate, total from subsequent columns
                        remaining = row[i+1:] if i+1 < len(row) else []
                        for j, rv in enumerate(remaining):
                            if rv is None:
                                continue
                            if isinstance(rv, str) and rv.strip():
                                if unit is None:
                                    unit = rv.strip()
                            if isinstance(rv, (int, float)):
                                if rate is None:
                                    rate = rv
                                elif total is None:
                                    total = rv
                        break

                # Look for description (string with equipment keywords)
                if desc is None and isinstance(val, str) and len(s) > 3:
                    upper = s.upper()
                    # Check if this looks like an equipment description
                    for keyword, _, _ in BOQ_KEYWORD_MAP:
                        if keyword in upper:
                            desc = s
                            break

            if desc and qty and qty > 0:
                # Classify the description
                equip_type = None
                equip_label = desc
                upper_desc = desc.upper()
                for keyword, etype, label in BOQ_KEYWORD_MAP:
                    if keyword in upper_desc:
                        equip_type = etype
                        equip_label = label
                        break

                items.append({
                    'description': desc,
                    'equipment_type': equip_type,
                    'equipment_label': equip_label,
                    'qty': qty,
                    'unit': unit,
                    'rate': rate,
                    'total': total,
                })

        os.unlink(tmp_path)
        return items

    except Exception as e:
        os.unlink(tmp_path)
        raise e


def compare_boq_vs_drawing(boq_items, drawing_merged):
    """
    Compare BOQ items against drawing detection results.
    Returns list of comparison rows.
    """
    comparisons = []

    # Aggregate BOQ by equipment type
    boq_by_type = {}
    for item in boq_items:
        etype = item['equipment_type']
        if etype:
            if etype not in boq_by_type:
                boq_by_type[etype] = {
                    'total_qty': 0,
                    'items': [],
                    'total_cost': 0,
                    'avg_rate': 0,
                }
            boq_by_type[etype]['total_qty'] += item['qty']
            boq_by_type[etype]['items'].append(item)
            if item.get('total'):
                boq_by_type[etype]['total_cost'] += item['total']
            if item.get('rate'):
                boq_by_type[etype]['avg_rate'] = item['rate']

    # Compare each BOQ type against drawing
    all_types = set(boq_by_type.keys()) | set(drawing_merged.keys())

    for etype in sorted(all_types):
        boq_data = boq_by_type.get(etype, {})
        drawing_data = drawing_merged.get(etype, {})

        boq_qty = boq_data.get('total_qty', 0)
        drawing_qty = drawing_data.get('count', 0)
        rate = boq_data.get('avg_rate', 0)

        diff = drawing_qty - boq_qty
        exposure = abs(diff) * rate if rate else 0

        # Determine risk level
        if boq_qty == 0 and drawing_qty > 0:
            risk = 'MISSING FROM BOQ'
        elif drawing_qty == 0 and boq_qty > 0:
            risk = 'NOT IN DRAWING'
        elif diff == 0:
            risk = 'MATCH'
        elif abs(diff) / max(boq_qty, 1) > 0.2:
            risk = 'HIGH'
        elif abs(diff) / max(boq_qty, 1) > 0.1:
            risk = 'MEDIUM'
        elif diff != 0:
            risk = 'LOW'
        else:
            risk = 'MATCH'

        name = etype.replace('_', ' ').title()

        comparisons.append({
            'Equipment': name,
            'BOQ Qty': int(boq_qty) if boq_qty else '—',
            'Drawing Qty': int(drawing_qty) if drawing_qty else '—',
            'Difference': f"{diff:+d}" if isinstance(boq_qty, (int, float)) and isinstance(drawing_qty, (int, float)) else '—',
            'Risk': risk,
            'Exposure (AED)': f"{exposure:,.0f}" if exposure > 0 else '—',
        })

    return comparisons

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
        # Save uploaded file to temp location
        # Detect file type from uploaded filename
        file_ext = os.path.splitext(drawing_file.name)[1].lower() or '.dxf'
        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
            tmp.write(drawing_file.read())
            tmp_path = tmp.name

        try:
            # Run the engine
            engine = TraceQEngine()
            result = engine.analyze(tmp_path)

            # Clean up temp file
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

    # Build table data
    table_data = []
    for equip_type, data in sorted(merged.items()):
        count = data.get('count', 0)
        source = data.get('source', 'unknown')
        confidence = data.get('confidence', 0)
        alt = data.get('alternate_counts', {})

        # Format source name
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

        # Format equipment name
        name = equip_type.replace('_', ' ').title()

        # Alternate counts string
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
        st.markdown("### 📊 BOQ vs Drawing Comparison")

        try:
            boq_bytes = boq_file.read()
            boq_items = parse_boq(boq_bytes, boq_file.name)

            if boq_items:
                comparisons = compare_boq_vs_drawing(boq_items, merged)

                # Summary metrics
                matches = sum(1 for c in comparisons if c['Risk'] == 'MATCH')
                issues = sum(1 for c in comparisons if c['Risk'] not in ('MATCH', '—'))
                total_exposure = sum(
                    float(c['Exposure (AED)'].replace(',', ''))
                    for c in comparisons
                    if c['Exposure (AED)'] != '—'
                )

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Items Matching", matches)
                with col2:
                    st.metric("Discrepancies", issues)
                with col3:
                    st.metric("Total Exposure", f"AED {total_exposure:,.0f}")

                # Color-code the comparison table
                st.dataframe(
                    comparisons,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Equipment": st.column_config.TextColumn("Equipment", width="medium"),
                        "BOQ Qty": st.column_config.TextColumn("BOQ", width="small"),
                        "Drawing Qty": st.column_config.TextColumn("Drawing", width="small"),
                        "Difference": st.column_config.TextColumn("Diff", width="small"),
                        "Risk": st.column_config.TextColumn("Risk", width="small"),
                        "Exposure (AED)": st.column_config.TextColumn("Exposure", width="small"),
                    }
                )

                # Show parsed BOQ items for debugging
                with st.expander("📄 Parsed BOQ Items", expanded=False):
                    boq_display = []
                    for item in boq_items:
                        boq_display.append({
                            "Description": item['description'][:60],
                            "Type": (item['equipment_type'] or 'unknown').replace('_', ' ').title(),
                            "Qty": item['qty'],
                            "Rate": item.get('rate', '—'),
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
            # Handle both string warnings and dict warnings
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

    # ─── Download Button ──────────────────────────────────────────────────────
    st.markdown("---")
    json_output = json.dumps(result.to_dict(), indent=2)
    st.download_button(
        label="📥 Download Full Analysis (JSON)",
        data=json_output,
        file_name=f"TraceQ_Analysis_{drawing_file.name}_{datetime.now().strftime('%Y%m%d')}.json",
        mime="application/json",
    )
