from app.services.pdf_extractor import Block
from app.services.hierarchy_parser import parse_blocks

def test_skipped_subsection_number_keeps_level_and_parent():
    blocks = [Block("2. Physical and Electrical Specifications",1),
              Block("2.1 General Specifications",1),
              Block("2.1.1.1 Battery Life Under Typical Use",2),
              Block("Four AA batteries provide 250 cycles.",2)]
    nodes = parse_blocks(blocks)
    irregular = next(n for n in nodes if "2.1.1.1" in n.heading)
    assert irregular.level == 4
    assert irregular.parent_key == next(n.temp_key for n in nodes if n.heading.startswith("2.1 General"))

def test_out_of_order_subsection_is_not_misparented():
    blocks = [Block("3. Device Operation",1),Block("3.1 Powering On",1),
              Block("3.4 Auto Shutoff",2),Block("3.3 Result Display",2)]
    nodes = parse_blocks(blocks)
    n34 = next(n for n in nodes if n.heading.startswith("3.4"))
    n33 = next(n for n in nodes if n.heading.startswith("3.3"))
    assert n34.parent_key == n33.parent_key
    assert n33.level == 2

def test_duplicate_headings_remain_distinct():
    blocks = [Block("4. Alarms and Safety Behavior",1),Block("4.2 Error Codes",1),Block("E1 cuff leak",1),
              Block("7. Troubleshooting",2),Block("7.1 Error Codes",2),Block("E5 sensor fault",2)]
    nodes = parse_blocks(blocks)
    duplicates = [n for n in nodes if n.heading.endswith("Error Codes")]
    assert len(duplicates) == 2
    assert duplicates[0].temp_key != duplicates[1].temp_key
    assert duplicates[0].parent_key != duplicates[1].parent_key

def test_lists_are_preserved_as_list_items():
    blocks = [Block("3.3 Result Display and Classification", 1), Block("1. Normal: systolic < 120", 1)]
    nodes = parse_blocks(blocks)
    assert "[LIST ITEM]" in nodes[0].body_text

def test_tables_preserved_as_markdown_structure():
    table_content = "| Code | Meaning | Device Behavior |\n|---|---|---|\n| E1 | Cuff leak | Aborts |"
    blocks = [
        Block("4. Alarms and Safety Behavior", 1),
        Block("4.2 Error Codes", 1),
        Block(table_content, 1, kind="table")
    ]
    nodes = parse_blocks(blocks)
    error_node = next(n for n in nodes if "4.2 Error Codes" in n.heading)
    assert "[TABLE]" in error_node.body_text
    assert "| Code | Meaning | Device Behavior |" in error_node.body_text
    assert "| E1 | Cuff leak | Aborts |" in error_node.body_text

def test_unicode_ligature_normalization():
    from app.services.pdf_extractor import normalize_text
    raw = "2. Physical and Electrical Speci\ufb01cations with Cu\ufb00 In\ufb02ation"
    cleaned = normalize_text(raw)
    assert cleaned == "2. Physical and Electrical Specifications with Cuff Inflation"
    assert "\ufb01" not in cleaned
    assert "\ufb00" not in cleaned
    assert "\ufb02" not in cleaned

def test_clinical_thresholds_not_mistaken_for_sections():
    blocks = [
        Block("3. Device Operation", 1),
        Block("3.3 Result Display and Classification", 1),
        Block("1. Normal: systolic < 120 and diastolic < 80", 1),
        Block("2. Elevated: systolic 120-129 and diastolic < 80", 1),
        Block("3. Hypertension Stage 1: systolic 130-139 or diastolic 80-89", 1),
        Block("4. Hypertension Stage 2: systolic >= 140 or diastolic >= 90", 1),
        Block("5. Hypertensive Crisis: systolic > 180 or diastolic > 120", 1),
    ]
    nodes = parse_blocks(blocks)
    # Should only produce section 3 and section 3.3, NOT five fake sections for the 1..5 items!
    section_headings = [n.heading for n in nodes if n.node_type == "section"]
    assert len(section_headings) == 2
    assert "3. Device Operation" in section_headings
    assert "3.3 Result Display and Classification" in section_headings
    assert "[LIST ITEM] 1. Normal:" in nodes[1].body_text
    assert "[LIST ITEM] 5. Hypertensive Crisis:" in nodes[1].body_text

def test_multipage_paragraph_continuation():
    blocks = [
        Block("3. Device Operation", 2),
        Block("3.1 Powering On and Profile Selection", 2),
        Block("Press and hold the power button for one second to power on the device.", 2),
        Block("Use the profile button to select User 1 or User 2 before beginning a measurement.", 3),
    ]
    nodes = parse_blocks(blocks)
    node_31 = next(n for n in nodes if "3.1" in n.heading)
    assert node_31.page_start == 2
    assert node_31.page_end == 3
    assert "Press and hold" in node_31.body_text
    assert "Use the profile button" in node_31.body_text

