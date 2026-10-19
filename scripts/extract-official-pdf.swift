// Native, offline PDF text extraction. The caller owns and removes temporary files.
import Foundation
import PDFKit

guard CommandLine.arguments.count == 2,
      let document = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else {
    fputs("Cannot open PDF\n", stderr); exit(2)
}
var pages: [[String: Any]] = []
for number in 0..<document.pageCount {
    guard let page = document.page(at: number) else { continue }
    pages.append(["page": number + 1, "text": page.string ?? ""])
}
let result: [String: Any] = ["schema": "ad.native-pdf-text.v1", "extractor": "macOS PDFKit", "page_count": document.pageCount, "pages": pages]
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]))
