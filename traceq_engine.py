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

# Try to import ezdxf (proper DXF library) — fall back to pure Python parser
try:
    import ezdxf
    HAS_EZDXF = True
except ImportError:
    HAS_EZDXF = False

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

    @property
    def duplicate_block_groups(self):
        """Config-driven duplicate block detection. Returns list of groups."""
        return self.block_dictionary.get("duplicate_block_groups", [])


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

        # Method 1: Try aspose-cad (Python library — works on all platforms)
        try:
            import aspose.cad as cad
            image = cad.Image.load(dwg_path)
            opts = cad.imageoptions.DxfOptions()
            image.save(dxf_path, opts)
            if os.path.exists(dxf_path):
                return dxf_path
        except (ImportError, Exception):
            pass

        # Method 2: Try LibreDWG command line (fallback)
        try:
            result = subprocess.run(
                ['dwg2dxf', '-o', dxf_path, dwg_path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and os.path.exists(dxf_path):
                return dxf_path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Method 3: Try ODA File Converter (fallback)
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
            f"Please convert manually: AutoCAD → File → Save As → DXF"
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
# EZDXF PARSER (Uses ezdxf library when available — better DXF + DWG support)
# ═══════════════════════════════════════════════════════════════════════════════

class EzdxfParser:
    """
    Full-featured DXF parser using the ezdxf library.
    Same interface as DXFParser so the rest of the engine works unchanged.
    Handles DXF files and can work with DWG files converted via libredwg.
    """

    def __init__(self, filepath):
        self.filepath = filepath
        self.layers = {}
        self.inserts = []
        self.mtext_entities = []
        self.text_entities = []
        self.all_entities = []
        self.block_definitions = {}
        self._total_entities = 0

    def parse(self):
        """Parse using ezdxf library."""
        doc = ezdxf.readfile(self.filepath)
        msp = doc.modelspace()

        # Extract layers
        for layer in doc.layers:
            self.layers[layer.dxf.name] = {
                'name': layer.dxf.name,
                'color': layer.dxf.color if hasattr(layer.dxf, 'color') else 7,
                'entity_count': 0,
            }

        # Extract entities from modelspace
        for entity in msp:
            self._total_entities += 1
            etype = entity.dxftype()
            layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else '0'

            self.all_entities.append({'type': etype, 'layer': layer})

            # Track entity count per layer
            if layer in self.layers:
                self.layers[layer]['entity_count'] += 1

            if etype == 'INSERT':
                block_name = entity.dxf.name if hasattr(entity.dxf, 'name') else ''
                if block_name:
                    x = entity.dxf.insert.x if hasattr(entity.dxf, 'insert') else 0
                    y = entity.dxf.insert.y if hasattr(entity.dxf, 'insert') else 0
                    self.inserts.append({
                        'name': block_name,
                        'layer': layer,
                        'x': x,
                        'y': y,
                    })

            elif etype == 'MTEXT':
                try:
                    raw = entity.text if hasattr(entity, 'text') else ''
                    # ezdxf can give us plain text directly
                    try:
                        clean = entity.plain_text() if hasattr(entity, 'plain_text') else raw
                    except Exception:
                        clean = DXFParser._clean_mtext(raw)
                    if clean:
                        x = entity.dxf.insert.x if hasattr(entity.dxf, 'insert') else 0
                        y = entity.dxf.insert.y if hasattr(entity.dxf, 'insert') else 0
                        self.mtext_entities.append({
                            'text': clean,
                            'raw_text': raw,
                            'layer': layer,
                            'x': x,
                            'y': y,
                        })
                except Exception:
                    pass

            elif etype == 'TEXT':
                try:
                    text = entity.dxf.text if hasattr(entity.dxf, 'text') else ''
                    if text:
                        x = entity.dxf.insert.x if hasattr(entity.dxf, 'insert') else 0
                        y = entity.dxf.insert.y if hasattr(entity.dxf, 'insert') else 0
                        self.text_entities.append({
                            'text': text,
                            'layer': layer,
                            'x': x,
                            'y': y,
                        })
                except Exception:
                    pass

        # Extract block definitions
        for block in doc.blocks:
            name = block.name
            entities = 0
            block_layers = set()
            for e in block:
                entities += 1
                if hasattr(e.dxf, 'layer'):
                    block_layers.add(e.dxf.layer)
            self.block_definitions[name] = {
                'entity_count': entities,
                'layers_used': list(block_layers),
            }

        return self

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
            has_exact_token = False
            for kw in keywords:
                hit = False
                # Token-level matching
                for token in tokens:
                    if kw == token:
                        hit = True
                        has_exact_token = True
                        break
                    if kw in token:
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
                # Exact token match floor: if a keyword matches a token exactly
                # (e.g., layer contains "VCD" as a distinct token), ensure minimum
                # score of 0.4. An exact abbreviation is a strong signal even if
                # only one keyword out of many matches.
                if has_exact_token and score < 0.4:
                    score = 0.4
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
# SIZE EXTRACTOR (Universal sub-type/size detection)
# ═══════════════════════════════════════════════════════════════════════════════

class SizeExtractor:
    """
    Universal size/dimension extraction from block names and text labels.

    Parses strings like "VCD 200", "250x250", "Ø200", "300mm dia", "FD-250"
    to extract size information. This allows automatic sub-type splitting
    without hardcoding specific sizes per equipment type.

    Config-driven: size_patterns in block_dictionary.json define what to look for.
    """

    # Universal regex patterns for size extraction (no hardcoding)
    SIZE_PATTERNS = [
        # "VCD 200" → diameter 200
        (r'(\d+)\s*(?:mm)?\s*(?:dia|diameter|Ø)', 'diameter'),
        # "Ø200" or "Ø 200"
        (r'Ø\s*(\d+)', 'diameter'),
        # "250x250" or "250 x 250" → rectangular
        (r'(\d+)\s*[xX×]\s*(\d+)', 'rectangular'),
        # "FD-300" or "VCD-200" → size from suffix
        (r'[A-Z]+-(\d{2,4})', 'suffix_size'),
        # Block name ending with number: "VCD 200" → 200
        (r'\s(\d{2,4})$', 'trailing_number'),
        # "200mm" standalone
        (r'(\d{2,4})\s*mm\b', 'millimetres'),
    ]

    @classmethod
    def extract_size(cls, text):
        """
        Extract size/dimension from a string. Returns dict or None.

        Returns:
            {'type': 'diameter', 'value': 200, 'raw': '200mm dia'}
            {'type': 'rectangular', 'width': 250, 'height': 250, 'raw': '250x250'}
            None if no size found
        """
        if not text:
            return None

        text = text.strip()

        # Try rectangular first (most specific)
        match = re.search(r'(\d+)\s*[xX×]\s*(\d+)', text)
        if match:
            return {
                'type': 'rectangular',
                'width': int(match.group(1)),
                'height': int(match.group(2)),
                'raw': match.group(0),
            }

        # Try diameter patterns
        match = re.search(r'Ø\s*(\d+)', text)
        if match:
            return {
                'type': 'diameter',
                'value': int(match.group(1)),
                'raw': match.group(0),
            }

        match = re.search(r'(\d+)\s*(?:mm)?\s*(?:dia|diameter)', text, re.IGNORECASE)
        if match:
            return {
                'type': 'diameter',
                'value': int(match.group(1)),
                'raw': match.group(0),
            }

        # Try suffix size (e.g., "FD-300")
        match = re.search(r'[A-Za-z]+-(\d{2,4})$', text)
        if match:
            return {
                'type': 'suffix_size',
                'value': int(match.group(1)),
                'raw': match.group(0),
            }

        # Try trailing number (e.g., "VCD 200")
        match = re.search(r'\s(\d{2,4})$', text)
        if match:
            return {
                'type': 'trailing_number',
                'value': int(match.group(1)),
                'raw': match.group(0),
            }

        # Try standalone mm (e.g., "200mm")
        match = re.search(r'(\d{2,4})\s*mm\b', text, re.IGNORECASE)
        if match:
            return {
                'type': 'millimetres',
                'value': int(match.group(1)),
                'raw': match.group(0),
            }

        return None

    @classmethod
    def format_size(cls, size_info):
        """Format extracted size for display."""
        if not size_info:
            return ''
        if size_info['type'] == 'rectangular':
            return f"{size_info['width']}x{size_info['height']}mm"
        elif size_info['type'] == 'diameter':
            return f"Ø{size_info['value']}mm"
        elif 'value' in size_info:
            return f"{size_info['value']}mm"
        return size_info.get('raw', '')


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
        self._last_conflicts = []  # Populated by _merge()

    def detect(self, parser: DXFParser):
        """Run all three tiers, deduplicate, and merge results."""
        tier1 = self._tier1_layers(parser)
        tier2 = self._tier2_blocks(parser)
        tier3 = self._tier3_mtext(parser)

        # Universal proximity deduplication:
        # When a text entity (Tier 3) is near a block INSERT (Tier 2) and both
        # map to the same equipment type, the text is a label for that block —
        # not a separate item. Reduce the Tier 3 count accordingly.
        dedup_report = self._proximity_dedup(parser, tier2, tier3)

        merged = self._merge(tier1, tier2, tier3)
        return {
            'tier1': tier1,
            'tier2': tier2,
            'tier3': tier3,
            'merged': merged,
            'dedup_report': dedup_report,
        }

    def _proximity_dedup(self, parser, tier2, tier3):
        """
        Universal proximity-based deduplication.

        Compares block INSERT positions (Tier 2) against text entity positions (Tier 3).
        When a text entity is within a configurable radius of a block that maps to the
        same equipment type, the text count is reduced — the text is a label, not a
        separate item.

        This is universal — works for ANY equipment type, not just specific blocks.
        The radius is auto-calculated from the drawing's bounding box (1% of diagonal).

        Returns a dedup_report dict for audit trail.
        """
        dedup_report = {
            'method': 'proximity_dedup',
            'adjustments': [],
            'radius_used': 0,
        }

        # Build a map of equipment_type → list of block INSERT positions from Tier 2
        block_defs = self.config.blocks
        prefix_rules = self.config.block_prefix_rules

        # For each insert, determine its equipment type
        block_positions_by_type = defaultdict(list)
        for insert in parser.inserts:
            block_name = insert['name']
            equip_type = None

            # Check exact match
            if block_name in block_defs:
                equip_type = block_defs[block_name]['equipment_type']
            else:
                # Check prefix rules
                for prefix, rule in prefix_rules.items():
                    if block_name.startswith(prefix) or block_name.upper().startswith(prefix.upper()):
                        equip_type = rule.get('default_type')
                        break

            if equip_type:
                block_positions_by_type[equip_type].append(
                    (insert.get('x', 0), insert.get('y', 0))
                )

        if not block_positions_by_type:
            return dedup_report

        # Auto-calculate radius from drawing bounding box
        # Use 1% of the diagonal as the proximity threshold
        all_x = [ins.get('x', 0) for ins in parser.inserts if ins.get('x', 0) != 0]
        all_y = [ins.get('y', 0) for ins in parser.inserts if ins.get('y', 0) != 0]
        if all_x and all_y:
            dx = max(all_x) - min(all_x)
            dy = max(all_y) - min(all_y)
            diagonal = (dx**2 + dy**2) ** 0.5
            radius = max(diagonal * 0.01, 50)  # At least 50 units, 1% of diagonal
        else:
            radius = 500  # Fallback
        dedup_report['radius_used'] = round(radius, 1)

        # Build a map of equipment_type → text pattern matches from config
        mtext_patterns = self.config.mtext_patterns
        text_type_map = {}  # pattern → equipment_type
        for equip_type, pdef in mtext_patterns.items():
            for pattern in pdef.get('patterns', []):
                text_type_map[pattern] = equip_type

        # For each text/mtext entity, check if it matches an equipment pattern
        # AND is near a block of the same type
        all_text_entities = parser.mtext_entities + parser.text_entities

        # Count how many text entities per type are "shadowed" by nearby blocks
        shadowed_counts = defaultdict(int)

        for text_ent in all_text_entities:
            tx = text_ent.get('x', 0)
            ty = text_ent.get('y', 0)
            text_content = text_ent.get('text', '')

            # What equipment type does this text match?
            matched_type = None
            for pattern, equip_type in text_type_map.items():
                try:
                    if re.search(pattern, text_content, re.IGNORECASE):
                        matched_type = equip_type
                        break
                except re.error:
                    continue

            if not matched_type:
                continue

            # Is there a block of the same type within radius?
            block_positions = block_positions_by_type.get(matched_type, [])
            for bx, by in block_positions:
                dist = ((tx - bx)**2 + (ty - by)**2) ** 0.5
                if dist <= radius:
                    shadowed_counts[matched_type] += 1
                    break  # Only count one shadow per text entity

        # Adjust Tier 3 counts by subtracting shadowed text entities
        for equip_type, shadow_count in shadowed_counts.items():
            if equip_type in tier3:
                original = tier3[equip_type]['count']
                adjusted = max(0, original - shadow_count)
                if adjusted != original:
                    dedup_report['adjustments'].append({
                        'equipment_type': equip_type,
                        'tier3_original': original,
                        'shadowed_by_blocks': shadow_count,
                        'tier3_adjusted': adjusted,
                        'note': f'{shadow_count} text labels near blocks of same type — likely labels, not separate items.',
                    })
                    tier3[equip_type]['count'] = adjusted
                    tier3[equip_type]['items'].append({
                        'dedup_adjustment': f'-{shadow_count} (proximity dedup: text near block within {radius:.0f} units)',
                    })

        return dedup_report

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

        # Track duplicate block groups from config (universal, not hardcoded)
        duplicate_groups = self.config.duplicate_block_groups

        # Exact block name matches
        for block_name, count in block_counts.items():
            if block_name in block_defs:
                defn = block_defs[block_name]
                equip_type = defn.get('equipment_type')
                confidence = defn.get('confidence', 0.85)

                # Skip blocks explicitly marked as non-equipment (null type)
                if not equip_type:
                    continue

                # Universal duplicate block handling:
                # If this block is a secondary in a duplicate group, skip it
                # (the primary block's count is the correct one)
                skip = False
                for group in duplicate_groups:
                    if block_name in group.get('secondary_blocks', []):
                        primary = group.get('primary_block', '')
                        if primary in block_counts:
                            skip = True
                            # Record as audit trail but don't add to count
                            results[equip_type]['items'].append({
                                'block_name': block_name,
                                'count': count,
                                'detection': f'tier2_block:{block_name} (duplicate of {primary} — excluded)',
                                'notes': group.get('reason', 'Duplicate block — count excluded.'),
                            })
                            break
                if skip:
                    continue

                # Extract size/dimension from block name (universal)
                size_info = SizeExtractor.extract_size(block_name)
                size_label = SizeExtractor.format_size(size_info) if size_info else ''

                results[equip_type]['count'] += count
                results[equip_type]['confidence'] = confidence
                item_data = {
                    'block_name': block_name,
                    'count': count,
                    'detection': f'tier2_block:{block_name}',
                    'notes': defn.get('confidence_note', ''),
                }
                if size_info:
                    item_data['size_info'] = size_info
                    item_data['size_label'] = size_label
                results[equip_type]['items'].append(item_data)

        # Prefix-based matching for unknown blocks
        for block_name, count in block_counts.items():
            if block_name in block_defs:
                continue  # Already matched
            for prefix, rule in prefix_rules.items():
                if block_name.startswith(prefix) or block_name.upper().startswith(prefix.upper()):
                    default_type = rule.get('default_type')
                    if default_type:
                        # Check config-driven duplicate groups (same logic as exact match)
                        skip = False
                        for group in duplicate_groups:
                            if block_name in group.get('secondary_blocks', []):
                                primary = group.get('primary_block', '')
                                if primary in block_counts:
                                    skip = True
                                    results[default_type]['items'].append({
                                        'block_name': block_name,
                                        'count': count,
                                        'detection': f'tier2_prefix:{prefix} (duplicate of {primary} — excluded)',
                                        'notes': group.get('reason', 'Duplicate block — count excluded.'),
                                    })
                                    break
                        if skip:
                            break

                        # Extract size/dimension from block name (universal)
                        size_info = SizeExtractor.extract_size(block_name)
                        size_label = SizeExtractor.format_size(size_info) if size_info else ''

                        confidence = rule.get('confidence', 0.7)
                        results[default_type]['count'] += count
                        results[default_type]['confidence'] = min(
                            results[default_type]['confidence'], confidence
                        )
                        item_data = {
                            'block_name': block_name,
                            'count': count,
                            'detection': f'tier2_prefix:{prefix}',
                        }
                        if size_info:
                            item_data['size_info'] = size_info
                            item_data['size_label'] = size_label
                        results[default_type]['items'].append(item_data)
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
        Merge three tiers with universal confidence-based logic.

        For each equipment type, selects the highest-confidence source
        with tier priority as tiebreaker (Tier 1 > Tier 2 > Tier 3).

        NO HARDCODED OVERRIDES. All special handling is config-driven:
          - Duplicate blocks: handled via duplicate_block_groups in config
          - Count disagreements: flagged for QS review (needs_review=True)
          - All three tier counts preserved in alternate_counts for audit

        When tiers disagree by >50%, the item is flagged for QS manual
        verification rather than silently picking a winner.
        """
        merged = {}

        # Collect all equipment types across tiers
        all_types = set(tier1.keys()) | set(tier2.keys()) | set(tier3.keys())

        # --- Conflict detection ---
        # When different tiers classify the same entities under different equipment types,
        # flag for QS review instead of silently reclassifying.
        conflicts = []

        for equip_type in all_types:
            t1 = tier1.get(equip_type, {})
            t2 = tier2.get(equip_type, {})
            t3 = tier3.get(equip_type, {})

            t1_count = t1.get('count', 0)
            t2_count = t2.get('count', 0)
            t3_count = t3.get('count', 0)

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

                # Check for significant count disagreements between tiers
                # If two tiers disagree by >50%, flag for QS review
                tier_counts = [c for c in [t1_count, t2_count, t3_count] if c > 0]
                needs_review = False
                review_note = ''
                if len(tier_counts) >= 2:
                    max_c = max(tier_counts)
                    min_c = min(tier_counts)
                    if max_c > 0 and (max_c - min_c) / max_c > 0.5:
                        needs_review = True
                        review_note = (
                            f'QS REVIEW: Tier counts disagree significantly '
                            f'(Layer: {t1_count}, Block: {t2_count}, Text: {t3_count}). '
                            f'Possible causes: duplicate entities, mislabelled layers, '
                            f'or block+text representing same item. '
                            f'Recommended: QS manual verification.'
                        )
                        conflicts.append({
                            'equipment_type': equip_type,
                            'tier1_count': t1_count,
                            'tier2_count': t2_count,
                            'tier3_count': t3_count,
                            'note': review_note,
                        })

                # When tiers disagree significantly AND Tier 1 (layer) is available,
                # prefer Tier 1. Layers are drawn intentionally by the engineer to
                # represent specific equipment types — they're the most deliberate
                # classification. Blocks can be shared across types (overcounting),
                # and text can be noisy. If Tier 1 is not available, keep the
                # highest-confidence winner as usual.
                final_count = best_data['count']
                final_source = best_source
                final_conf = best_conf
                final_items = best_data.get('items', [])

                if needs_review and t1_count > 0 and best_source != 'tier1_layer':
                    # Tier 1 has data but wasn't the winner — override to Tier 1
                    final_count = t1_count
                    final_source = 'tier1_layer'
                    final_conf = t1_conf
                    final_items = t1.get('items', [])
                    review_note += (
                        f' → Merge overrode to Layer count ({t1_count:,}) '
                        f'because tiers disagree and layers are engineer-assigned.'
                    )

                merged[equip_type] = {
                    'count': final_count,
                    'source': final_source,
                    'confidence': final_conf,
                    'items': final_items,
                    'alternate_counts': {
                        'tier1': t1_count,
                        'tier2': t2_count,
                        'tier3': t3_count,
                    },
                    'needs_review': needs_review,
                }
                if review_note:
                    merged[equip_type]['notes'] = review_note

        # Store conflicts for the report
        self._last_conflicts = conflicts

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

    @property
    def detection_results(self):
        return self.detection

    @property
    def validation_results(self):
        return {
            'is_valid': self.validation[0],
            'violations': self.validation[1],
            'warnings': self.validation[2],
        }

    @property
    def parse_info(self):
        return {
            'total_entities': self.parser.total_entities,
            'layers': len(self.parser.layers),
            'block_types': len(self.parser.insert_counts_by_block),
            'inserts': len(self.parser.inserts),
            'mtext': len(self.parser.mtext_entities),
        }

    @property
    def layer_classification(self):
        """Get layer classification from the engine's classifier."""
        config = Config()
        classifier = LayerClassifier(config)
        return classifier.classify_all_layers(self.parser.layers.keys())

    def to_dict(self):
        """Export results as a dictionary."""
        return self.to_json()

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

            # Show all tier counts
            alts = data.get('alternate_counts', {})
            t1 = alts.get('tier1', 0)
            t2 = alts.get('tier2', 0)
            t3 = alts.get('tier3', 0)
            lines.append(f"    Tier counts — Layer: {t1}, Block: {t2}, Text: {t3}")

            # Flag for QS review if tiers disagree
            if data.get('needs_review', False):
                lines.append(f"    ⚠️  FLAGGED FOR QS REVIEW — tier counts disagree significantly")
                if data.get('notes'):
                    lines.append(f"    Note: {data['notes']}")

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

        # Proximity dedup report
        dedup = self.detection.get('dedup_report', {})
        adjustments = dedup.get('adjustments', [])
        if adjustments:
            lines.append("\n" + "=" * 80)
            lines.append("PROXIMITY DEDUPLICATION")
            lines.append("=" * 80)
            lines.append(f"  Radius used: {dedup.get('radius_used', 0):.0f} units")
            for adj in adjustments:
                lines.append(
                    f"  {adj['equipment_type']}: Tier 3 reduced from {adj.get('tier3_original', 0)} "
                    f"to {adj.get('tier3_adjusted', 0)} "
                    f"({adj.get('shadowed_by_blocks', 0)} text labels near blocks)"
                )

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
# QUICK SCAN RESULT (Step 0)
# ═══════════════════════════════════════════════════════════════════════════════

class QuickScanResult:
    """Container for Step 0 quick scan compatibility results."""

    def __init__(self, filepath, total_layers, hvac_candidate_layers,
                 recognised_layers, unrecognised_layers, non_equipment_layers,
                 total_blocks, recognised_blocks, unrecognised_blocks,
                 mtext_count, mtext_pattern_hits, total_mtext_patterns,
                 layer_score, block_score, mtext_score, overall_score,
                 verdict, verdict_msg, total_entities, total_inserts):
        self.filepath = filepath
        self.total_layers = total_layers
        self.hvac_candidate_layers = hvac_candidate_layers
        self.recognised_layers = recognised_layers
        self.unrecognised_layers = unrecognised_layers
        self.non_equipment_layers = non_equipment_layers
        self.total_blocks = total_blocks
        self.recognised_blocks = recognised_blocks
        self.unrecognised_blocks = unrecognised_blocks
        self.mtext_count = mtext_count
        self.mtext_pattern_hits = mtext_pattern_hits
        self.total_mtext_patterns = total_mtext_patterns
        self.layer_score = layer_score
        self.block_score = block_score
        self.mtext_score = mtext_score
        self.overall_score = overall_score
        self.verdict = verdict
        self.verdict_msg = verdict_msg
        self.total_entities = total_entities
        self.total_inserts = total_inserts
        self.timestamp = datetime.now().isoformat()
        self._dwg_unsupported = False

    @classmethod
    def dwg_not_supported(cls, filepath):
        """Return a result indicating DWG conversion is not available."""
        result = cls(
            filepath=filepath, total_layers=0, hvac_candidate_layers=0,
            recognised_layers=[], unrecognised_layers=[], non_equipment_layers=[],
            total_blocks=0, recognised_blocks=[], unrecognised_blocks=[],
            mtext_count=0, mtext_pattern_hits=0, total_mtext_patterns=0,
            layer_score=0, block_score=0, mtext_score=0, overall_score=0,
            verdict='DWG_UNSUPPORTED',
            verdict_msg='DWG file detected but no converter is installed. Please convert to DXF first (AutoCAD → Save As → DXF).',
            total_entities=0, total_inserts=0,
        )
        result._dwg_unsupported = True
        return result

    def summary(self):
        """Human-readable summary for display."""
        lines = []
        lines.append("=" * 70)
        lines.append("TRACEQ STEP 0 — QUICK COMPATIBILITY SCAN")
        lines.append("=" * 70)
        lines.append(f"File: {os.path.basename(self.filepath)}")
        lines.append(f"Scan Date: {self.timestamp}")
        lines.append(f"Total Entities: {self.total_entities:,}")
        lines.append(f"Total INSERTs: {self.total_inserts:,}")
        lines.append("")
        lines.append(f"OVERALL COMPATIBILITY: {self.overall_score}% — {self.verdict}")
        lines.append(f"{self.verdict_msg}")
        lines.append("")
        lines.append(f"Layers:  {len(self.recognised_layers)}/{self.hvac_candidate_layers} recognised ({self.layer_score}%)")
        lines.append(f"Blocks:  {len(self.recognised_blocks)}/{self.total_blocks} recognised ({self.block_score}%)")
        lines.append(f"Text:    {self.mtext_pattern_hits}/{self.total_mtext_patterns} patterns found ({self.mtext_score}%)")
        lines.append("")

        if self.recognised_layers:
            lines.append("Recognised layers:")
            for rl in self.recognised_layers:
                lines.append(f"  ✓ {rl['layer']} → {rl['equipment_type']} ({rl['confidence']:.0%})")

        if self.unrecognised_layers:
            lines.append("Unrecognised layers (may contain equipment):")
            for ul in self.unrecognised_layers:
                lines.append(f"  ? {ul}")

        if self.recognised_blocks:
            lines.append("Recognised blocks:")
            for rb in self.recognised_blocks:
                lines.append(f"  ✓ {rb['block']} → {rb['equipment_type']} (×{rb['count']})")

        if self.unrecognised_blocks:
            lines.append("Unrecognised blocks:")
            for ub in self.unrecognised_blocks:
                lines.append(f"  ? {ub['block']} (×{ub['count']})")

        lines.append("")
        lines.append("=" * 70)
        return '\n'.join(lines)

    def to_dict(self):
        """Export as dictionary."""
        return {
            'filepath': self.filepath,
            'timestamp': self.timestamp,
            'overall_score': self.overall_score,
            'verdict': self.verdict,
            'verdict_msg': self.verdict_msg,
            'layer_score': self.layer_score,
            'block_score': self.block_score,
            'mtext_score': self.mtext_score,
            'recognised_layers': self.recognised_layers,
            'unrecognised_layers': self.unrecognised_layers,
            'recognised_blocks': self.recognised_blocks,
            'unrecognised_blocks': [dict(ub) for ub in self.unrecognised_blocks],
            'total_entities': self.total_entities,
            'total_inserts': self.total_inserts,
        }


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

    def quick_scan(self, filepath):
        """
        Step 0: Quick compatibility scan.
        Parses the DXF/DWG file and checks how many layers and blocks
        the engine recognises against its config files.
        Returns a QuickScanResult with compatibility scores.
        """
        filepath = os.path.abspath(filepath)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")

        # Handle DWG — for quick scan, we still need DXF
        file_type = FileConverter.detect_type(filepath)
        if file_type == 'dwg':
            try:
                filepath = FileConverter.convert_dwg_to_dxf(filepath)
            except RuntimeError:
                return QuickScanResult.dwg_not_supported(filepath)
        elif file_type != 'dxf':
            raise ValueError(f"Unsupported file type: {file_type}")

        # Parse with pure Python parser (lightweight, no dependencies)
        parser = DXFParser(filepath)
        parser.parse()

        # --- Layer compatibility ---
        layer_names = list(parser.layers.keys())
        ignore_patterns = self.config.ignore_layer_patterns

        # Filter out obvious non-equipment layers (TEXT, DIM, XREF, etc.)
        hvac_candidate_layers = []
        non_equipment_layers = []
        for lname in layer_names:
            upper = lname.upper()
            is_ignored = False
            for pat in ignore_patterns:
                if pat.upper() in upper and not any(
                    eq in upper for eq in ['DIFF', 'EQUIP', 'FCU', 'VCD', 'FD', 'THERMO']
                ):
                    is_ignored = True
                    break
            if is_ignored:
                non_equipment_layers.append(lname)
            else:
                hvac_candidate_layers.append(lname)

        # Classify candidate layers
        recognised_layers = []
        unrecognised_layers = []
        for lname in hvac_candidate_layers:
            equip_type, confidence, method = self.classifier.classify(lname)
            if equip_type and confidence >= 0.3:
                recognised_layers.append({
                    'layer': lname,
                    'equipment_type': equip_type,
                    'confidence': confidence,
                    'method': method,
                })
            else:
                unrecognised_layers.append(lname)

        # --- Block compatibility ---
        block_names = list(parser.insert_counts_by_block.keys())
        known_blocks = self.config.blocks
        prefix_rules = self.config.block_prefix_rules

        recognised_blocks = []
        unrecognised_blocks = []
        for bname in block_names:
            count = parser.insert_counts_by_block[bname]
            # Check exact match
            if bname in known_blocks:
                recognised_blocks.append({
                    'block': bname,
                    'equipment_type': known_blocks[bname]['equipment_type'],
                    'count': count,
                    'match': 'exact',
                })
                continue
            # Check prefix rules
            prefix_matched = False
            for prefix, rule in prefix_rules.items():
                if bname.startswith(prefix) or bname.upper().startswith(prefix.upper()):
                    if rule.get('default_type'):
                        recognised_blocks.append({
                            'block': bname,
                            'equipment_type': rule['default_type'],
                            'count': count,
                            'match': f'prefix:{prefix}',
                        })
                        prefix_matched = True
                        break
            if not prefix_matched:
                # Skip blocks that are clearly not equipment (low entity count, model space, etc.)
                if bname.startswith('*Model') or bname.startswith('*Paper'):
                    continue
                unrecognised_blocks.append({
                    'block': bname,
                    'count': count,
                })

        # --- MTEXT compatibility ---
        mtext_count = len(parser.mtext_entities) + len(parser.text_entities)
        mtext_patterns = self.config.mtext_patterns
        mtext_hits = 0
        all_text = '\n'.join(
            [mt['text'] for mt in parser.mtext_entities] +
            [t['text'] for t in parser.text_entities]
        )
        for equip_type, pdef in mtext_patterns.items():
            for pattern in pdef.get('patterns', []):
                try:
                    if re.search(pattern, all_text, re.IGNORECASE):
                        mtext_hits += 1
                        break
                except re.error:
                    continue

        # --- Compute scores ---
        total_candidate_layers = len(hvac_candidate_layers)
        layer_score = (len(recognised_layers) / total_candidate_layers * 100) if total_candidate_layers > 0 else 0

        total_blocks = len(block_names)
        block_score = (len(recognised_blocks) / total_blocks * 100) if total_blocks > 0 else 0

        total_mtext_patterns = len(mtext_patterns)
        mtext_score = (mtext_hits / total_mtext_patterns * 100) if total_mtext_patterns > 0 else 0

        # Overall compatibility: weighted average (layers 40%, blocks 40%, mtext 20%)
        overall = (layer_score * 0.4) + (block_score * 0.4) + (mtext_score * 0.2)

        # Determine verdict
        if overall >= 60:
            verdict = 'HIGH'
            verdict_msg = 'Good compatibility. TraceQ should produce reliable results. Nestor review will be light.'
        elif overall >= 30:
            verdict = 'MEDIUM'
            verdict_msg = 'Moderate compatibility. TraceQ will catch common items but some will be missed. Nestor should expect more corrections.'
        else:
            verdict = 'LOW'
            verdict_msg = 'Low compatibility. This project uses naming conventions TraceQ hasn\'t seen before. Nestor\'s corrections will significantly expand the dictionary.'

        return QuickScanResult(
            filepath=filepath,
            total_layers=len(layer_names),
            hvac_candidate_layers=total_candidate_layers,
            recognised_layers=recognised_layers,
            unrecognised_layers=unrecognised_layers,
            non_equipment_layers=non_equipment_layers,
            total_blocks=total_blocks,
            recognised_blocks=recognised_blocks,
            unrecognised_blocks=unrecognised_blocks,
            mtext_count=mtext_count,
            mtext_pattern_hits=mtext_hits,
            total_mtext_patterns=total_mtext_patterns,
            layer_score=round(layer_score, 1),
            block_score=round(block_score, 1),
            mtext_score=round(mtext_score, 1),
            overall_score=round(overall, 1),
            verdict=verdict,
            verdict_msg=verdict_msg,
            total_entities=parser.total_entities,
            total_inserts=len(parser.inserts),
        )

    def analyze(self, filepath):
        """
        Analyze a DWG or DXF file.
        Returns an AnalysisResult object.
        """
        filepath = os.path.abspath(filepath)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")

        # Step 1: Handle file type — convert DWG to DXF if needed
        file_type = FileConverter.detect_type(filepath)
        if file_type == 'dwg':
            print(f"[TraceQ] DWG file detected. Converting to DXF...")
            filepath = FileConverter.convert_dwg_to_dxf(filepath)
            print(f"[TraceQ] Converted: {filepath}")
        elif file_type == 'dxf':
            print(f"[TraceQ] DXF file: {os.path.basename(filepath)}")
        else:
            raise ValueError(f"Unsupported file type: {file_type}")

        # Step 2: Parse DXF — use ezdxf if available, otherwise pure Python
        print(f"[TraceQ] Parsing entities...")
        if HAS_EZDXF:
            print(f"[TraceQ] Using ezdxf library (full feature parser)")
            parser = EzdxfParser(filepath)
        else:
            print(f"[TraceQ] Using pure Python parser (ezdxf not available)")
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
