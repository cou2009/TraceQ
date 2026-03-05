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
import tempfile
from datetime import datetime

# Import the TraceQ engine (same directory)
from traceq_engine import TraceQEngine, Config

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

    # ─── Validation Results ───────────────────────────────────────────────────
    st.markdown("### ⚠️ Validation Checks")

    validation = result.validation_results
    warnings = validation.get('warnings', [])

    if not warnings:
        st.success("All validation checks passed — no warnings.")
    else:
        for w in warnings:
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
