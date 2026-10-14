"""Synthetic real-format documents, with no project/user data."""

import io
import struct
import zipfile
import zlib

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
    + png_chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 16) * 16))
    + png_chunk(b"IEND", b"")
)


def pdf(pages: int = 1, padding: int = 0) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for number in range(1, pages + 1):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 72 720 Td (SYNTHETIC-PAGE-{number}) Tj ET".encode())
        page[NameObject("/Contents")] = content
    if padding:
        stream = DecodedStreamObject()
        stream.set_data(b"x" * padding)
        writer._root_object[NameObject("/SyntheticPadding")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def visual_docx() -> bytes:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
 xmlns:v="urn:schemas-microsoft-com:vml"><w:body>
<w:p><w:r><w:t>COUNTER-CORRECTION</w:t></w:r></w:p>
<w:tbl><w:tblPr><w:tblW w:w="6000" w:type="dxa"/></w:tblPr>
<w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>
<w:tr><w:tc><w:p><w:r><w:t>BEFORE -1</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>AFTER 0</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="914400" cy="914400"/>
<wp:docPr id="1" name="Synthetic red image"/><a:graphic>
<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="red.png"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>
</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:r><w:pict><v:rect id="rect1" style="width:160pt;height:36pt" fillcolor="#aaccff">
<v:textbox><w:txbxContent><w:p><w:r><w:t>SHAPE-MARKER</w:t></w:r></w:p>
</w:txbxContent></v:textbox></v:rect></w:pict></w:r></w:p>
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
<w:p><w:r><w:t>VALIDATION-PAGE-2</w:t></w:r></w:p>
<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>
<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
</w:body></w:document>"""
    types = """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    package_rels = """<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
 Target="word/document.xml"/>
</Relationships>"""
    document_rels = """<Relationships
 xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
 Target="media/red.png"/>
</Relationships>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in {
            "[Content_Types].xml": types,
            "_rels/.rels": package_rels,
            "word/document.xml": xml,
            "word/_rels/document.xml.rels": document_rels,
            "word/media/red.png": PNG,
        }.items():
            archive.writestr(name, body)
    return output.getvalue()
