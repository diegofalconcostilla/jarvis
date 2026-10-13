// Typesets a document (JSON list of styled blocks) with macOS AppKit and writes a PDF and/or DOCX.
// Usage: osascript -l JavaScript render.js spec.json out.pdf|- out.docx|- [margin_inches]
// spec: {"blocks": [...]} or {"source": "/path/file.html|.docx"}; optional "landscape": true
// Prints {"pages": <how many Letter pages the text fills>}. Nothing to install: AppKit ships with macOS.
ObjC.import('AppKit');
function font(b) {
  var name = b.bold ? (b.italic ? 'Arial-BoldItalicMT' : 'Arial-BoldMT') : (b.italic ? 'Arial-ItalicMT' : 'ArialMT');
  return $.NSFont.fontWithNameSize(name, b.size || 10.5);
}
function run(argv) {
  var spec = JSON.parse($.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null).js);
  var pw = spec.landscape ? 11 * 72 : 8.5 * 72, ph = spec.landscape ? 8.5 * 72 : 11 * 72;
  var margin = parseFloat(argv[3] || '0.6') * 72, width = pw - 2 * margin, pageH = ph - 2 * margin;
  var doc = $.NSMutableAttributedString.alloc.init;
  if (spec.source) {
    doc.setAttributedString($.NSAttributedString.alloc.initWithURLOptionsDocumentAttributesError(
      $.NSURL.fileURLWithPath(spec.source), $({}), null, null));
  }
  (spec.blocks || []).forEach(function (b, i) {
    var ps = $.NSMutableParagraphStyle.alloc.init;
    ps.paragraphSpacingBefore = b.before || 0;
    ps.paragraphSpacing = b.after || 0;
    ps.lineHeightMultiple = b.line || 1.0;
    var tabs = $.NSMutableArray.alloc.init;
    if (b.indent) {  // hanging indent: "•\ttext"
      ps.headIndent = b.indent; ps.firstLineHeadIndent = 0;
      tabs.addObject($.NSTextTab.alloc.initWithTextAlignmentLocationOptions(0, b.indent, $({})));
    }
    if (b.right !== undefined || b.rule) {  // right-aligned part / full-width rule
      tabs.addObject($.NSTextTab.alloc.initWithTextAlignmentLocationOptions(2, width, $({})));
    }
    ps.tabStops = tabs;
    if (b.align === 'center') ps.alignment = 1;
    var color = $.NSColor.colorWithCalibratedWhiteAlpha(b.gray || 0, 1);
    var attrs = $({}), main = (b.text || '');
    var para = $.NSMutableAttributedString.alloc.init;
    function add(text, extra) {
      var d = $.NSMutableDictionary.alloc.init;
      d.setObjectForKey(font(extra), $.NSFontAttributeName);
      d.setObjectForKey(ps, $.NSParagraphStyleAttributeName);
      d.setObjectForKey(extra.gray ? $.NSColor.colorWithCalibratedWhiteAlpha(extra.gray, 1) : color, $.NSForegroundColorAttributeName);
      if (extra.underline) d.setObjectForKey($(1), $.NSUnderlineStyleAttributeName);
      var start = para.length;  // (initWithString:attributes: isn't bridged to JXA; append, then style the range)
      para.mutableString.appendString(text);
      para.setAttributesRange(d, $.NSMakeRange(start, para.length - start));
    }
    (b.runs || [{text: main}]).forEach(function (r) {
      add(r.text, {size: r.size || b.size, bold: r.bold !== undefined ? r.bold : b.bold,
                   italic: r.italic !== undefined ? r.italic : b.italic, gray: r.gray !== undefined ? r.gray : b.gray,
                   underline: b.rule});
    });
    if (b.right !== undefined) add('\t' + b.right, {size: b.rsize || b.size, italic: b.ritalic, bold: false, gray: b.rgray});
    else if (b.rule) add('\t', {size: b.size, underline: true});
    if (i < spec.blocks.length - 1) add('\n', {size: b.size});
    doc.appendAttributedString(para);
  });
  var tv = $.NSTextView.alloc.initWithFrame($.NSMakeRect(0, 0, width, pageH));
  tv.textContainerInset = $.NSMakeSize(0, 0);
  tv.textContainer.lineFragmentPadding = 0;
  tv.textStorage.setAttributedString(doc);
  tv.layoutManager.glyphRangeForTextContainer(tv.textContainer);
  var used = tv.layoutManager.usedRectForTextContainer(tv.textContainer).size.height;
  tv.setFrameSize($.NSMakeSize(width, Math.ceil(used) + 1));  // text height: no blank trailing page
  if (argv[1] && argv[1] !== '-') {
    var pi = $.NSPrintInfo.sharedPrintInfo.copy;
    pi.paperSize = $.NSMakeSize(pw, ph);
    if (spec.landscape) pi.orientation = 1;
    pi.topMargin = margin; pi.bottomMargin = margin; pi.leftMargin = margin; pi.rightMargin = margin;
    pi.verticallyCentered = false; pi.horizontallyCentered = false;
    pi.jobDisposition = $.NSPrintSaveJob;
    pi.dictionary.setObjectForKey($.NSURL.fileURLWithPath(argv[1]), $.NSPrintJobSavingURL);
    var op = $.NSPrintOperation.printOperationWithViewPrintInfo(tv, pi);
    op.showsPrintPanel = false; op.showsProgressPanel = false;
    op.runOperation;
  }
  if (argv[2] && argv[2] !== '-') {
    var da = $.NSMutableDictionary.alloc.init;
    da.setObjectForKey($.NSOfficeOpenXMLTextDocumentType, $.NSDocumentTypeDocumentAttribute);
    var data = doc.dataFromRangeDocumentAttributesError($.NSMakeRange(0, doc.length), da, null);
    data.writeToFileAtomically(argv[2], true);
  }
  return JSON.stringify({pages: used / pageH});
}
