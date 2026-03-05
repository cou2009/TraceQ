#!/usr/bin/env python3
"""
TraceQ Analysis Engine v1.0
===========================
Unified HVAC equipment detection from DWG/DXF drawings.
Three-tier detection: Layer → Block → MTEXT
Built by TechTelligence — nicholas@ttelligence.com

Usage:
    engine = TraceQEngine()
    results = engine.analyze("path/to/drawing.dxf")
    print(results.summary())
    results.to_json("output.json")
"""

import json
import os
import re
import struct
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION LOADER
# ═══════════════════════════════════════════════════════════════════════════════

class Config:
    """Loads and provides access to layer standards and block dictionary."""

    def __init__(self, config_dir=None):
        if config_dir is None:
            config_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_dir = config_dir
        self.layer_standards = self._load_json("traceq_layer_standards.json")
        self.block_dictionary = self._load_json("traceq_block_dictionary.json")

    def _load_json(self, filename):
        path = os.path.join(self.config_dir, filename)
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}

    @property
    def equipment_categories(self):
        return self.layer_standards.get("equipment_categories", {})

    @property
    def ignore_layer_patterns(self):
        return self.layer_standards.get("ignore_layers", {}).get("patterns", [])

    @property
    def blocks(self):
        return self.block_dictionary.get("blocks", {})

    @property
    def block_prefix_rules(self):
        return self.block_dictionary.get("block_prefix_rules", {})

    @property
    def mtext_patterns(self):
        return self.block_dictionary.get("mtext_patterns", {})

    @property
    def validation_rules(self):
        return self.block_dictionary.get("validation_rules", {})


# ═══════════════════════════════════════════════════════════════════════════════
# FILE CONVERTER (DWG → DXF)
# ═══════════════════════════════════════════════════════════════════════════════

class FileConverter:
    """Handles DWG to DXF conversion. Falls back gracefully if tools unavailable."""

    @staticmethod
    def detect_type(filepath):
        ext = Path(filepath).suffix.lower()
        if ext == '.dwg':
            return 'dwg'
        elif ext == '.dxf':
            return 'dxf'
        # Check magic bytes
        with open(filepath, 'rb') as f:
            header = f.read(6)
        if header[:2] == b'AC':
            return 'dwg'
        if b'SECTION' in open(filepath, 'rb').read(100):
            return 'dxf'
        return 'unknown'

    @staticmethod
    def convert_dwg_to_dxf(dwg_path, output_dir=None):
        """Convert DWG to DXF using available tools."""
        if output_dir is None:
            output_dir = os.path.dirname(dwg_path)

        dxf_path = os.path.join(output_dir, Path(dwg_path).stem + '.dxf')

        # Try LibreDWG
        try:
            result = subprocess.run(
                ['dwg2dxf', '-o', dxf_path, dwg_path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and os.path.exists(dxf_path):
                return dxf_path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Try ODA File Converter
        try:
            result = subprocess.run(
                ['ODAFileConverter', os.path.dirname(dwg_path), output_dir,
                 'ACAD2018', 'DXF', '0', '1', Path(dwg_path).name],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and os.path.exists(dxf_path):
                return dxf_path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        raise RuntimeError(
            f"Cannot convert DWG to DXF. No converter available.\n"
            f"Install libredwg-tools (apt) or ODA File Converter.\n"
            f"Or convert manually: AutoCAD → Save As → DXF"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# DXF PARSER (Pure Python — no external dependencies)
# ═══════════════════════════════════════════════════════════════════════════════

class DXFParser:
    """
    Extracts entities from DXF files using group code pair parsing.
    Proven approach from TraceQ samples 3-6.
    """

    def __init__(self, filepath):
        self.filepath = filepath
        self.layers = {}          # {layer_name: {color, entity_count}}
        self.inserts = []         # [{name, layer, x, y, ...}]
        self.mtext_entities = []  # [{text, layer, x, y}]
        self.text_entities = []   # [{text, layer, x, y}]
        self.all_entities = []    # [{type, layer, ...}]
        self.block_definitions = {}  # {block_name: {entity_count, layers_used}}
        self._total_entities = 0

    def parse(self):
        """Parse the DXF file and extract all relevant data."""
        pairs = self._read_group_pairs()
        self._extract_layers(pairs)
        self._extract_entities(pairs)
        self._extract_block_definitions(pairs)
        return self

    def _read_group_pairs(self):
        """Read DXF as group code / value pairs."""
        pairs = []
        try:
            with open(self.filepath, 'r', errors='replace') as f:
                lines = f.readlines()
        except UnicodeDecodeError:
            with open(self.filepath, 'rb') as f:
                content = f.read().decode('utf-8', errors='replace')
                lines = content.splitlines(True)

        i = 0
        while i < len(lines) - 1:
            try:
                code = int(lines[i].strip())
                value = lines[i + 1].strip()
                pairs.append((code, value))
            except (ValueError, IndexError):
                pass
            i += 2
        return pairs

    def _extract_layers(self, pairs):
        """Extract layer definitions from TABLES section."""
        in_layer_table = False
        current_layer = None

        for i, (code, value) in enumerate(pairs):
            if code == 2 and value == 'LAYER':
                in_layer_table = True
                continue
            if in_layer_table and code == 0 and value == 'ENDTAB':
                break
            if in_layer_table and code == 0 and value == 'LAYER':
                if current_layer:
                    self.layers[current_layer['name']] = current_layer
                current_layer = {'name': '', 'color': 7, 'entity_count': 0}
            if current_layer:
                if code == 2:
                    current_layer['name'] = value
                elif code == 62:
                    try:
                        current_layer['color'] = int(value)
                    except ValueError:
                        pass

        if current_layer and current_layer.get('name'):
            self.layers[current_layer['name']] = current_layer

    def _extract_entities(self, pairs):
        """Extract INSERT, MTEXT, TEXT entities from ENTITIES section."""
        in_entities = False
        current_entity = None
        current_type = None
        mtext_buffer = []

        for i, (code, value) in enumerate(pairs):
            if code == 2 and value == 'ENTITIES':
                in_entities = True
                continue
            if code == 0 and value == 'ENDSEC' and in_entities:
                break

            if not in_entities:
                continue

            if code == 0:
                # Save previous entity
                if current_entity and current_type:
                    self._save_entity(current_type, current_entity, mtext_buffer)
                current_type = value
                current_entity = {'type': value, 'layer': '0'}
                mtext_buffer = []
                self._total_entities += 1
                continue

            if current_entity is None:
                continue

            if code == 8:
                current_entity['layer'] = value
            elif code == 2 and current_type == 'INSERT':
                current_entity['block_name'] = value
            elif code == 10:
                try:
                    current_entity['x'] = float(value)
                except ValueError:
                    pass
            elif code == 20:
                try:
                    current_entity['y'] = float(value)
                except ValueError:
                    pass
            elif code == 1 and current_type in ('TEXT', 'ATTRIB'):
                current_entity['text'] = value
            elif code == 1 and current_type == 'MTEXT':
                mtext_buffer.append(value)
            elif code == 3 and current_type == 'MTEXT':
                mtext_buffer.append(value)

        # Save last entity
        if current_entity and current_type:
            self._save_entity(current_type, current_entity, mtext_buffer)

    def _save_entity(self, entity_type, entity, mtext_buffer):
        """Process and store a completed entity."""
        layer = entity.get('layer', '0')

        if entity_type == 'INSERT':
            block_name = entity.get('block_name', '')
            if block_name:
                self.inserts.append({
                    'name': block_name,
                    'layer': layer,
                    'x': entity.get('x', 0),
                    'y': entity.get('y', 0),
                })
        elif entity_type == 'MTEXT':
            raw_text = ''.join(mtext_buffer)
            clean_text = self._clean_mtext(raw_text)
            if clean_text:
                self.mtext_entities.append({
                    'text': clean_text,
                    'raw_text': raw_text,
                    'layer': layer,
                    'x': entity.get('x', 0),
                    'y': entity.get('y', 0),
                })
        elif entity_type == 'TEXT':
            text = entity.get('text', '')
            if text:
                self.text_entities.append({
                    'text': text,
                    'layer': layer,
                    'x': entity.get('x', 0),
                    'y': entity.get('y', 0),
                })

        self.all_entities.append({
            'type': entity_type,
            'layer': layer,
        })

    def _extract_block_definitions(self, pairs):
        """Extract block definitions from BLOCKS section."""
        in_blocks = False
        current_block = None
        block_name = None

        for i, (code, value) in enumerate(pairs):
            if code == 2 and value == 'BLOCKS':
                in_blocks = True
                continue
            if code == 0 and value == 'ENDSEC' and in_blocks:
                break
            if not in_blocks:
                continue

            if code == 0 and value == 'BLOCK':
                current_block = {'entities': 0, 'layers': set()}
                block_name = None
            elif code == 0 and value == 'ENDBLK':
                if block_name and current_block:
                    self.block_definitions[block_name] = {
                        'entity_count': current_block['entities'],
                        'layers_used': list(current_block['layers']),
                    }
                current_block = None
            elif current_block is not None:
                if code == 2 and block_name is None:
                    block_name = value
                elif code == 0:
                    current_block['entities'] += 1
                elif code == 8:
                    current_block['layers'].add(value)

    @staticmethod
    def _clean_mtext(raw):
        """Remove DXF formatting codes from MTEXT."""
        text = raw
        text = re.sub(r'\\f[^;]*;', '', text)        # Font codes
        text = re.sub(r'\\[HWQATpo][^;]*;', '', text) # Height, width, etc.
        text = re.sub(r'\\[LlOoKk]', '', text)        # Underline, overline, strikethrough
        text = re.sub(r'\\P', '\n', text)              # Paragraph breaks
        text = re.sub(r'\\~', ' ', text)               # Non-breaking space
        text = text.replace('\\\\', '\\')                # Escaped backslash
        text = re.sub(r'\{|\}', '', text)              # Braces
        text = re.sub(r'\\[A-Za-z][^;]*;', '', text)  # Remaining codes
        return text.strip()

    @property
    def total_entities(self):
        return self._total_entities

    @property
    def entity_counts_by_layer(self):
        counts = Counter()
        for e in self.all_entities:
            counts[e['layer']] += 1
        return dict(counts)

    @property
    def insert_counts_by_block(self):
        counts = Counter()
        for ins in self.inserts:
            counts[ins['name']] += 1
        return dict(counts)


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER CLASSIFIER (Tier 1)
# ═══════════════════════════════════════════════════════════════════════════════

class LayerClassifier:
    """
    Maps layer names to equipment categories using fuzzy matching.
    Uses Nestor's HVAC + NHM standards as reference.
    """

    def __init__(self, config: Config):
        self.categories = config.equipment_categories
        self.ignore_patterns = config.ignore_layer_patterns
        self._cache = {}

    def classify(self, layer_name):
        """
        Classify a layer name into an equipment category.
        Returns: (equipment_type, confidence, match_method) or (None, 0, 'no_match')
        """
        if layer_name in self._cache:
            return self._cache[layer_name]

        result = self._do_classify(layer_name)
        self._cache[layer_name] = result
        return result

    def _do_classify(self, layer_name):
        if self._should_ignore(layer_name):
            return (None, 0, 'ignored')

        upper = layer_name.upper().strip()

        # Pass 1: Exact match against known layers
        for cat_name, cat_def in self.categories.items():
            for exact in cat_def.get('exact_layers', []):
                if exact.upper() == upper:
                    return (cat_name, 1.0, 'exact')

        # Pass 2: Token-based keyword matching
        tokens = re.split(r'[-_ /\\.]+', upper)
        # Filter out very short tokens that cause false matches
        tokens = [t for t in tokens if len(t) >= 2]
        best_match = None
        best_score = 0

        for cat_name, cat_def in self.categories.items():
            keywords = [k.upper() for k in cat_def.get('keywords', [])]
            excludes = [k.upper() for k in cat_def.get('exclude_keywords', [])]

            if not keywords:
                continue

            # Check exclusions first
            excluded = False
            for exc in excludes:
                if exc in upper:
                    excluded = True
                    break
            if excluded:
                continue

            # Count keyword hits (each keyword counted at most once)
            hits = 0
            for kw in keywords:
                hit = False
                # Token-level matching
                for token in tokens:
                    if kw == token or kw in token:
                        hit = True
                        break
                    # Allow token-in-keyword only for tokens >= 3 chars
                    if len(token) >= 3 and token in kw:
                        hit = True
                        break
                # Full string match as fallback
                if not hit and kw in upper:
                    hit = True
                if hit:
                    hits += 1

            if hits > 0:
                # Score = proportion of keywords matched, weighted by specificity
                score = min(hits / max(len(keywords), 1), 1.0)
                # Bonus for HVAC-prefixed layers (more likely to be equipment)
                if 'HVAC' in upper or upper.startswith('M-'):
                    score = min(score + 0.15, 1.0)
                if score > best_score:
                    best_score = score
                    best_match = cat_name

        if best_match and best_score >= 0.3:
            confidence = round(min(best_score, 0.95), 2)
            return (best_match, confidence, 'keyword')

        return (None, 0, 'no_match')

    def _should_ignore(self, layer_name):
        upper = layer_name.upper()
        for pattern in self.ignore_patterns:
            if pattern.upper() in upper and not any(
                eq in upper for eq in ['DIFF', 'EQUIP', 'FCU', 'VCD', 'FD', 'THERMO']
            ):
                return True
        return False

    def classify_all_layers(self, layer_names):
        """Classify a list of layer names. Returns dict of results."""
        results = {}
        for name in layer_names:
            equip_type, confidence, method = self.classify(name)
            results[name] = {
                'equipment_type': equip_type,
                'confidence': confidence,
                'method': method,
            }
        return results


# ═══════════════════════════════════════════════════════════════════════════════
# EQUIPMENT DETECTOR (Three-Tier)
# ═══════════════════════════════════════════════════════════════════════════════

class EquipmentDetector:
    """
    Three-tier equipment detection with merging logic.
    Tier 1: Layer-based (confidence 1.0)
    Tier 2: Block name (confidence 0.85)
    Tier 3: MTEXT patterns (confidence 0.6)
    """

    def __init__(self, config: Config, classifier: LayerClassifier):
        self.config = config
        self.classifier = classifier

    def detect(self, parser: DXFParser):
        """Run all three tiers and merge results."""
        tier1 = self._tier1_layers(parser)
        tier2 = self._tier2_blocks(parser)
        tier3 = self._tier3_mtext(parser)
        merged = self._merge(tier1, tier2, tier3)
        return {
            'tier1': tier1,
            'tier2': tier2,
            'tier3': tier3,
            'merged': merged,
        }

    def _tier1_layers(self, parser: DXFParser):
        """Tier 1: Count entities by classified layer."""
        results = defaultdict(lambda: {'items': [], 'count': 0, 'source': 'tier1_layer', 'confidence': 1.0})

        # Classify all layers
        layer_classes = self.classifier.classify_all_layers(parser.layers.keys())

        # Count INSERT entities by their classified layer
        for insert in parser.inserts:
            layer = insert.get('layer', '0')
            cls = layer_classes.get(layer, {})
            equip_type = cls.get('equipment_type')
            confidence = cls.get('confidence', 0)

            if equip_type and confidence >= 0.5:
                results[equip_type]['count'] += 1
                results[equip_type]['confidence'] = confidence
                results[equip_type]['items'].append({
                    'block_name': insert['name'],
                    'layer': layer,
                    'x': insert.get('x', 0),
                    'y': insert.get('y', 0),
                    'detection': f'tier1_layer:{layer}',
                })

        return dict(results)

    def _tier2_blocks(self, parser: DXFParser):
        """Tier 2: Match INSERT block names against dictionary."""
        results = defaultdict(lambda: {'items': [], 'count': 0, 'source': 'tier2_block', 'confidence': 0.85})
        block_counts = parser.insert_counts_by_block
        block_defs = self.config.blocks
        prefix_rules = self.config.block_prefix_rules

        # Exact block name matches
        for block_name, count in block_counts.items():
            if block_name in block_defs:
                defn = block_defs[block_name]
                equip_type = defn['equipment_type']
                confidence = defn.get('confidence', 0.85)

                # Special handling: *U20 is duplicate of VCD 200 — skip to avoid double-count
                if block_name == '*U20' and 'VCD 200' in block_counts:
                    continue

                results[equip_type]['count'] += count
                results[equip_type]['confidence'] = confidence
                results[equip_type]['items'].append({
                    'block_name': block_name,
                    'count': count,
                    'detection': f'tier2_block:{block_name}',
                    'notes': defn.get('confidence_note', ''),
                })

        # Prefix-based matching for unknown blocks
        for block_name, count in block_counts.items():
            if block_name in block_defs:
                continue  # Already matched
            for prefix, rule in prefix_rules.items():
                if block_name.startswith(prefix) or block_name.upper().startswith(prefix.upper()):
                    default_type = rule.get('default_type')
                    if default_type:
                        confidence = rule.get('confidence', 0.7)
                        results[default_type]['count'] += count
                        results[default_type]['confidence'] = min(
                            results[default_type]['confidence'], confidence
                        )
                        results[default_type]['items'].append({
                            'block_name': block_name,
                            'count': count,
                            'detection': f'tier2_prefix:{prefix}',
                        })
                    break

        return dict(results)

    def _tier3_mtext(self, parser: DXFParser):
        """Tier 3: Pattern matching on MTEXT/TEXT content."""
        results = defaultdict(lambda: {'items': [], 'count': 0, 'source': 'tier3_mtext', 'confidence': 0.6})
        pattern_defs = self.config.mtext_patterns

        # Combine all text content
        all_text = []
        for mt in parser.mtext_entities:
            all_text.append(mt['text'])
        for t in parser.text_entities:
            all_text.append(t['text'])
        combined = '\n'.join(all_text)

        for equip_type, pdef in pattern_defs.items():
            patterns = pdef.get('patterns', [])
            method = pdef.get('count_method', 'count_occurrences')
            confidence = pdef.get('confidence', 0.6)

            matches = set()
            max_occurrences = 0  # Track per-pattern max to avoid double-counting
            for pattern in patterns:
                try:
                    found = re.findall(pattern, combined, re.IGNORECASE)
                    matches.update(found)
                    max_occurrences = max(max_occurrences, len(found))
                except re.error:
                    continue

            if not matches:
                continue

            if method == 'count_unique_labels':
                count = len(matches)
            elif method == 'count_unique_labels_div2':
                count = len(matches) // 2
            elif method == 'count_occurrences':
                # Use max across patterns (not sum) to avoid double-counting
                # when overlapping patterns match the same text
                count = max_occurrences
            else:
                count = len(matches)

            if count > 0:
                results[equip_type]['count'] = count
                results[equip_type]['confidence'] = confidence
                results[equip_type]['items'].append({
                    'pattern_matches': list(matches)[:20],
                    'total_matches': len(matches),
                    'count_method': method,
                    'detection': f'tier3_mtext:{patterns[0]}',
                })

        return dict(results)

    def _merge(self, tier1, tier2, tier3):
        """
        Merge three tiers with hierarchy: Tier 1 > Tier 2 > Tier 3.
        For each equipment type, use the highest-confidence source.
        Special overrides:
          - supply_diffuser: *U19 overcounts; prefer SAD MTEXT count or Tier 1 layer
          - *U17 = return_diffuser per Nestor (extract under AC = RAD)
        """
        merged = {}

        # Collect all equipment types across tiers
        all_types = set(tier1.keys()) | set(tier2.keys()) | set(tier3.keys())

        for equip_type in all_types:
            t1 = tier1.get(equip_type, {})
            t2 = tier2.get(equip_type, {})
            t3 = tier3.get(equip_type, {})

            t1_count = t1.get('count', 0)
            t2_count = t2.get('count', 0)
            t3_count = t3.get('count', 0)

            # --- Special override: supply_diffuser ---
            # Per Nestor: *U19 overcounts. Prefer SAD MTEXT label count when available.
            # If neither tier1 nor tier3 has data, fall back to tier2 (block) but flag it.
            if equip_type == 'supply_diffuser':
                # Check if Tier 3 found SAD labels
                if t3_count > 0:
                    merged[equip_type] = {
                        'count': t3_count,
                        'source': 'tier3_mtext (SAD label override)',
                        'confidence': 0.9,  # SAD is reliable per Nestor
                        'items': t3.get('items', []),
                        'alternate_counts': {'tier1': t1_count, 'tier2': t2_count},
                        'notes': 'Per Nestor: SAD MTEXT label count preferred over *U19 block count.'
                    }
                elif t1_count > 0:
                    merged[equip_type] = {
                        'count': t1_count,
                        'source': 'tier1_layer',
                        'confidence': t1.get('confidence', 1.0),
                        'items': t1.get('items', []),
                        'alternate_counts': {'tier2': t2_count, 'tier3': t3_count},
                    }
                elif t2_count > 0:
                    merged[equip_type] = {
                        'count': t2_count,
                        'source': 'tier2_block (WARNING: *U19 overcounts)',
                        'confidence': t2.get('confidence', 0.7),
                        'items': t2.get('items', []),
                        'alternate_counts': {'tier1': t1_count, 'tier3': t3_count},
                        'notes': 'WARNING: *U19 block count likely overcounts. No SAD MTEXT available.'
                    }
                continue

            # --- Smart merge: prefer highest confidence, with tier as tiebreaker ---
            t1_conf = t1.get('confidence', 0) if t1_count > 0 else 0
            t2_conf = t2.get('confidence', 0) if t2_count > 0 else 0
            t3_conf = t3.get('confidence', 0) if t3_count > 0 else 0

            # Build candidate list: (confidence, tier_priority, tier_data, source_name)
            # Higher tier_priority = preferred as tiebreaker (Tier 1 > 2 > 3)
            candidates = []
            if t1_count > 0:
                candidates.append((t1_conf, 3, t1, 'tier1_layer'))
            if t2_count > 0:
                candidates.append((t2_conf, 2, t2, 'tier2_block'))
            if t3_count > 0:
                candidates.append((t3_conf, 1, t3, 'tier3_mtext'))

            if candidates:
                # Sort by confidence (desc), then tier priority (desc)
                candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
                best_conf, _, best_data, best_source = candidates[0]
                merged[equip_type] = {
                    'count': best_data['count'],
                    'source': best_source,
                    'confidence': best_conf,
                    'items': best_data.get('items', []),
                    'alternate_counts': {
                        'tier1': t1_count,
                        'tier2': t2_count,
                        'tier3': t3_count,
                    }
                }

        return merged


# ═══════════════════════════════════════════════════════════════════════════════
# VALIDATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class ValidationEngine:
    """Applies cross-check rules to the equipment inventory."""

    def __init__(self, config: Config):
        self.config = config

    def validate(self, merged_inventory):
        """Run all validation rules. Returns (is_valid, violations, warnings)."""
        counts = self._extract_counts(merged_inventory)
        violations = []
        warnings = []

        # Rule 1: Thermostat = FCU count
        thermo = counts.get('thermostat', 0)
        fcu = counts.get('fcu', 0)
        if thermo > 0 and fcu > 0:
            tolerance = self.config.validation_rules.get(
                'thermostat_equals_fcu', {}
            ).get('tolerance_percent', 10) / 100
            if abs(thermo - fcu) > max(thermo, fcu) * tolerance:
                warnings.append({
                    'rule': 'thermostat_equals_fcu',
                    'message': f'Thermostat count ({thermo}) does not match FCU count ({fcu}). '
                               f'Per Nestor: thermostat qty should equal total FCU count.',
                    'severity': 'warning',
                    'thermostat_count': thermo,
                    'fcu_count': fcu,
                })
        elif thermo == 0 and fcu > 0:
            violations.append({
                'rule': 'thermostat_missing',
                'message': f'{fcu} FCUs detected but ZERO thermostats. Thermostats likely missing from drawing or BOQ.',
                'severity': 'error',
                'fcu_count': fcu,
            })

        # Rule 2: VCD should exist if FCUs present
        vcd = counts.get('volume_control_damper', 0)
        if fcu > 0 and vcd == 0:
            warnings.append({
                'rule': 'vcd_missing_with_fcu',
                'message': f'{fcu} FCUs detected but ZERO VCDs. VCDs are typically required at duct branches.',
                'severity': 'warning',
            })

        # Rule 3: Non-negative counts
        for equip_type, count in counts.items():
            if count < 0:
                violations.append({
                    'rule': 'negative_count',
                    'message': f'Negative count for {equip_type}: {count}',
                    'severity': 'error',
                })

        # Rule 4: Standard practice items reminder
        standard_items = self.config.validation_rules.get(
            'standard_practice_items', {}
        ).get('items', [])
        missing_standards = []
        for item in standard_items:
            mapped = item.replace('_', ' ').replace('vcds', 'volume_control_damper')
            # Check if any similar key exists
            found = False
            for k in counts:
                if item.replace('_', '') in k.replace('_', '') or k in item:
                    found = True
                    break
            if not found:
                missing_standards.append(item)

        if missing_standards:
            warnings.append({
                'rule': 'standard_practice_items',
                'message': f'Standard practice items not detected: {", ".join(missing_standards)}. '
                           f'Per Nestor: these should be included for accurate takeoff.',
                'severity': 'info',
                'missing_items': missing_standards,
            })

        return (len(violations) == 0, violations, warnings)

    def _extract_counts(self, merged):
        return {k: v.get('count', 0) for k, v in merged.items()}


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYSIS RESULT
# ═══════════════════════════════════════════════════════════════════════════════

class AnalysisResult:
    """Container for complete analysis results with output methods."""

    def __init__(self, filepath, parser, detection_results, validation_results):
        self.filepath = filepath
        self.parser = parser
        self.detection = detection_results
        self.validation = validation_results
        self.timestamp = datetime.now().isoformat()

    @property
    def merged(self):
        return self.detection.get('merged', {})

    def summary(self):
        """Generate human-readable summary string."""
        lines = []
        lines.append("=" * 80)
        lines.append("TRACEQ HVAC EQUIPMENT ANALYSIS REPORT")
        lines.append("=" * 80)
        lines.append(f"\nFile: {os.path.basename(self.filepath)}")
        lines.append(f"Analysis Date: {self.timestamp}")
        lines.append(f"Total Entities Parsed: {self.parser.total_entities:,}")
        lines.append(f"Layers Found: {len(self.parser.layers)}")
        lines.append(f"Block Types Found: {len(self.parser.insert_counts_by_block)}")
        lines.append(f"INSERT Entities: {len(self.parser.inserts):,}")
        lines.append(f"MTEXT Entities: {len(self.parser.mtext_entities):,}")

        lines.append("\n" + "=" * 80)
        lines.append("EQUIPMENT INVENTORY (Merged — Best Detection Per Type)")
        lines.append("=" * 80)

        total_items = 0
        for equip_type in sorted(self.merged.keys()):
            data = self.merged[equip_type]
            count = data['count']
            source = data['source']
            confidence = data.get('confidence', 0)
            total_items += count

            label = equip_type.replace('_', ' ').title()
            src_label = {
                'tier1_layer': 'Layer',
                'tier2_block': 'Block',
                'tier3_mtext': 'MTEXT',
            }.get(source, source)

            lines.append(f"\n  {label}:")
            lines.append(f"    Count: {count}")
            lines.append(f"    Source: Tier {source[4]} ({src_label}) — Confidence: {confidence:.0%}")

            # Show items detail
            for item in data.get('items', []):
                if 'block_name' in item and 'count' in item:
                    lines.append(f"    → {item['block_name']}: {item['count']} units")
                elif 'pattern_matches' in item:
                    samples = item['pattern_matches'][:5]
                    lines.append(f"    → Patterns matched: {', '.join(samples)}")
                    if item.get('total_matches', 0) > 5:
                        lines.append(f"      ... and {item['total_matches'] - 5} more")

            # Show alternate counts if different
            alts = data.get('alternate_counts', {})
            alt_strs = []
            for tier, alt_count in alts.items():
                if alt_count > 0 and alt_count != count:
                    alt_strs.append(f"{tier}: {alt_count}")
            if alt_strs:
                lines.append(f"    Alternate counts: {', '.join(alt_strs)}")

        lines.append(f"\n  TOTAL EQUIPMENT ITEMS: {total_items:,}")

        # Tier breakdown
        lines.append("\n" + "=" * 80)
        lines.append("DETECTION TIER SUMMARY")
        lines.append("=" * 80)
        for tier_name in ['tier1', 'tier2', 'tier3']:
            tier_data = self.detection.get(tier_name, {})
            tier_label = {'tier1': 'Tier 1 (Layer)', 'tier2': 'Tier 2 (Block)', 'tier3': 'Tier 3 (MTEXT)'}[tier_name]
            total = sum(v.get('count', 0) for v in tier_data.values())
            types = len(tier_data)
            lines.append(f"\n  {tier_label}: {total:,} items across {types} categories")
            for et, ed in sorted(tier_data.items()):
                lines.append(f"    {et}: {ed.get('count', 0)}")

        # Validation
        is_valid, violations, warnings = self.validation
        lines.append("\n" + "=" * 80)
        lines.append("VALIDATION RESULTS")
        lines.append("=" * 80)
        status = "PASS" if is_valid else "FAIL"
        lines.append(f"\n  Status: {status}")
        if violations:
            lines.append(f"  Violations ({len(violations)}):")
            for v in violations:
                lines.append(f"    [ERROR] {v['message']}")
        if warnings:
            lines.append(f"  Warnings ({len(warnings)}):")
            for w in warnings:
                lines.append(f"    [{w['severity'].upper()}] {w['message']}")
        if not violations and not warnings:
            lines.append("  No issues detected.")

        # Layer classification report
        lines.append("\n" + "=" * 80)
        lines.append("LAYER CLASSIFICATION")
        lines.append("=" * 80)
        layer_classes = {}
        classifier = LayerClassifier(Config())
        for layer_name in sorted(self.parser.layers.keys()):
            et, conf, method = classifier.classify(layer_name)
            if et:
                layer_classes[layer_name] = (et, conf, method)
                lines.append(f"  {layer_name} → {et} ({conf:.0%}, {method})")

        unclassified = [l for l in self.parser.layers if l not in layer_classes]
        if unclassified:
            lines.append(f"\n  Unclassified layers ({len(unclassified)}):")
            for l in sorted(unclassified):
                lines.append(f"    {l}")

        lines.append("\n" + "=" * 80)
        lines.append("END OF REPORT")
        lines.append("=" * 80)
        return '\n'.join(lines)

    def to_json(self, output_path=None):
        """Export results as JSON."""
        data = {
            'metadata': {
                'file_name': os.path.basename(self.filepath),
                'file_path': self.filepath,
                'analysis_date': self.timestamp,
                'engine_version': '1.0.0',
            },
            'file_info': {
                'total_entities': self.parser.total_entities,
                'layers': len(self.parser.layers),
                'block_types': len(self.parser.insert_counts_by_block),
                'insert_count': len(self.parser.inserts),
                'mtext_count': len(self.parser.mtext_entities),
                'text_count': len(self.parser.text_entities),
            },
            'equipment_inventory': {},
            'summary': {},
            'detection_audit': {
                'tier1_types': len(self.detection.get('tier1', {})),
                'tier2_types': len(self.detection.get('tier2', {})),
                'tier3_types': len(self.detection.get('tier3', {})),
            },
            'validation': {
                'is_valid': self.validation[0],
                'violations': self.validation[1],
                'warnings': self.validation[2],
            },
        }

        total = 0
        for equip_type, det in self.merged.items():
            count = det['count']
            total += count
            data['equipment_inventory'][equip_type] = {
                'count': count,
                'source': det['source'],
                'confidence': det.get('confidence', 0),
                'items': det.get('items', []),
                'alternate_counts': det.get('alternate_counts', {}),
            }
            data['summary'][equip_type] = count
        data['summary']['total'] = total

        if output_path:
            with open(output_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
        return data


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENGINE (Orchestrator)
# ═══════════════════════════════════════════════════════════════════════════════

class TraceQEngine:
    """
    Main entry point for TraceQ analysis.

    Usage:
        engine = TraceQEngine()
        result = engine.analyze("path/to/file.dxf")
        print(result.summary())
    """

    def __init__(self, config_dir=None):
        self.config = Config(config_dir)
        self.classifier = LayerClassifier(self.config)
        self.detector = EquipmentDetector(self.config, self.classifier)
        self.validator = ValidationEngine(self.config)

    def analyze(self, filepath):
        """
        Analyze a DWG or DXF file.
        Returns an AnalysisResult object.
        """
        filepath = os.path.abspath(filepath)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")

        # Step 1: Handle file type
        file_type = FileConverter.detect_type(filepath)
        if file_type == 'dwg':
            print(f"[TraceQ] DWG file detected. Converting to DXF...")
            filepath = FileConverter.convert_dwg_to_dxf(filepath)
            print(f"[TraceQ] Converted: {filepath}")
        elif file_type == 'dxf':
            print(f"[TraceQ] DXF file: {os.path.basename(filepath)}")
        else:
            raise ValueError(f"Unsupported file type: {file_type}")

        # Step 2: Parse DXF
        print(f"[TraceQ] Parsing entities...")
        parser = DXFParser(filepath)
        parser.parse()
        print(f"[TraceQ] Parsed: {parser.total_entities:,} entities, "
              f"{len(parser.layers)} layers, "
              f"{len(parser.inserts):,} blocks")

        # Step 3: Run three-tier detection
        print(f"[TraceQ] Running three-tier detection...")
        detection = self.detector.detect(parser)
        merged = detection['merged']
        total = sum(v.get('count', 0) for v in merged.values())
        print(f"[TraceQ] Detected: {total:,} equipment items across "
              f"{len(merged)} categories")

        # Step 4: Validate
        print(f"[TraceQ] Validating...")
        validation = self.validator.validate(merged)
        status = "PASS" if validation[0] else f"FAIL ({len(validation[1])} violations)"
        print(f"[TraceQ] Validation: {status}, {len(validation[2])} warnings")

        return AnalysisResult(filepath, parser, detection, validation)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python traceq_engine.py <file.dxf|file.dwg> [output.json]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_json = sys.argv[2] if len(sys.argv) > 2 else None

    engine = TraceQEngine()
    result = engine.analyze(input_file)

    print("\n" + result.summary())

    if output_json:
        result.to_json(output_json)
        print(f"\nJSON output saved: {output_json}")
