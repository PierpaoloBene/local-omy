// brain-extract: the text of a PDF or of an image, as JSON on stdout.
//
//   brain-extract FILE [--max-pages N] [--ocr-below CHARS]
//
// PDF pages are read with PDFKit; a page with less than --ocr-below characters
// of text (a scan) is rendered and read with Vision's text recognition (Italian
// and English). Images (PNG, JPEG, HEIC, TIFF) go straight to Vision.
// Everything stays on this Mac: PDFKit and Vision run locally.
//
// Output: {"pages": [{"n": 1, "text": "…", "ocr": false}, …], "error": null}

import AppKit
import Foundation
import PDFKit
import Vision

struct Page: Encodable {
    let n: Int
    let text: String
    let ocr: Bool
}

struct Output: Encodable {
    let pages: [Page]
    let error: String?
}

func emit(_ out: Output) -> Never {
    let data = try! JSONEncoder().encode(out)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
    exit(out.error == nil ? 0 : 1)
}

func recognize(_ image: CGImage) -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["it-IT", "en-US"]
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return ""
    }
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

func render(_ page: PDFPage) -> CGImage? {
    // About 150 dpi: enough for Vision on printed text. thumbnail() applies the page rotation.
    let box = page.bounds(for: .mediaBox)
    let scale: CGFloat = 150.0 / 72.0
    let image = page.thumbnail(of: NSSize(width: box.width * scale, height: box.height * scale), for: .mediaBox)
    var rect = CGRect(origin: .zero, size: image.size)
    return image.cgImage(forProposedRect: &rect, context: nil, hints: nil)
}

var args = Array(CommandLine.arguments.dropFirst())
var maxPages = 300
var ocrBelow = 30
if let i = args.firstIndex(of: "--max-pages"), i + 1 < args.count, let n = Int(args[i + 1]) {
    maxPages = n
    args.removeSubrange(i...(i + 1))
}
if let i = args.firstIndex(of: "--ocr-below"), i + 1 < args.count, let n = Int(args[i + 1]) {
    ocrBelow = n
    args.removeSubrange(i...(i + 1))
}
guard args.count == 1 else {
    emit(Output(pages: [], error: "usage: brain-extract FILE [--max-pages N] [--ocr-below CHARS]"))
}
let url = URL(fileURLWithPath: args[0])

if url.pathExtension.lowercased() == "pdf" {
    guard let doc = PDFDocument(url: url) else {
        emit(Output(pages: [], error: "cannot open the PDF"))
    }
    if doc.isLocked {
        emit(Output(pages: [], error: "the PDF is protected by a password"))
    }
    var pages: [Page] = []
    for i in 0..<min(doc.pageCount, maxPages) {
        guard let page = doc.page(at: i) else { continue }
        let text = page.string ?? ""
        if text.trimmingCharacters(in: .whitespacesAndNewlines).count >= ocrBelow {
            pages.append(Page(n: i + 1, text: text, ocr: false))
        } else if let image = render(page) {
            pages.append(Page(n: i + 1, text: recognize(image), ocr: true))
        }
    }
    emit(Output(pages: pages, error: nil))
}

guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    emit(Output(pages: [], error: "not a PDF nor an image"))
}
emit(Output(pages: [Page(n: 1, text: recognize(image), ocr: true)], error: nil))
