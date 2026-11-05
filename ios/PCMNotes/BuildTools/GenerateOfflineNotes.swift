import AppKit
import Foundation
import PDFKit

private struct CatalogChapter: Decodable {
    let number: Int
    let title: String
    let folderName: String
    let pageCount: Int
}

private struct ArchiveSubject {
    let slug: String
    let title: String
}

private let subjects = [
    ArchiveSubject(slug: "physics", title: "Physics"),
    ArchiveSubject(slug: "chemistry", title: "Chemistry"),
    ArchiveSubject(slug: "mathematics", title: "Mathematics"),
]

private enum GeneratorError: LocalizedError {
    case catalogMismatch(String)
    case invalidArguments
    case missingSource(URL)
    case unreadableImage(URL)
    case unwritablePage(URL)
    case unwritableDocument(URL)

    var errorDescription: String? {
        switch self {
        case .catalogMismatch(let message):
            "Chapter catalog does not match the source archive: \(message)"
        case .invalidArguments:
            "Usage: GenerateOfflineNotes <archive-root> <output-root>"
        case .missingSource(let url):
            "Missing notes source: \(url.path)"
        case .unreadableImage(let url):
            "Could not read note image: \(url.path)"
        case .unwritablePage(let url):
            "Could not convert note image into a PDF page: \(url.path)"
        case .unwritableDocument(let url):
            "Could not write chapter PDF: \(url.path)"
        }
    }
}

@main
private enum OfflineNotesGenerator {
    static func main() throws {
        guard CommandLine.arguments.count == 3 else {
            throw GeneratorError.invalidArguments
        }

        let fileManager = FileManager.default
        let archiveRoot = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let outputRoot = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        try fileManager.createDirectory(at: outputRoot, withIntermediateDirectories: true)

        let catalogData = try Data(contentsOf: archiveRoot.appendingPathComponent("catalog.json"))
        let catalog = try JSONDecoder().decode([String: [CatalogChapter]].self, from: catalogData)
        guard Set(catalog.keys) == Set(subjects.map(\.slug)) else {
            throw GeneratorError.catalogMismatch("expected Physics, Chemistry and Mathematics")
        }

        var manifestSubjects: [[String: Any]] = []
        var totalChapters = 0
        var totalPages = 0

        for subject in subjects {
            let subjectSource = archiveRoot.appendingPathComponent(subject.slug, isDirectory: true)
            guard fileManager.fileExists(atPath: subjectSource.path) else {
                throw GeneratorError.missingSource(subjectSource)
            }

            let subjectOutput = outputRoot.appendingPathComponent(subject.slug, isDirectory: true)
            try fileManager.createDirectory(at: subjectOutput, withIntermediateDirectories: true)

            let chapterFolders = try fileManager.contentsOfDirectory(
                at: subjectSource,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )
            .filter { (try? $0.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true }
            .sorted { chapterNumber(for: $0) < chapterNumber(for: $1) }

            let entries = catalog[subject.slug] ?? []
            guard entries.count == chapterFolders.count,
                  entries.map(\.number) == Array(1...max(entries.count, 1)),
                  entries.map(\.folderName) == chapterFolders.map(\.lastPathComponent) else {
                throw GeneratorError.catalogMismatch(subject.slug)
            }
            var subjectPages = 0

            for chapterFolder in chapterFolders {
                let chapterNumber = chapterNumber(for: chapterFolder)
                let imageURLs = try fileManager.contentsOfDirectory(
                    at: chapterFolder,
                    includingPropertiesForKeys: [.contentModificationDateKey],
                    options: [.skipsHiddenFiles]
                )
                .filter { ["jpg", "jpeg"].contains($0.pathExtension.lowercased()) }
                .sorted { $0.lastPathComponent < $1.lastPathComponent }

                let entry = entries[chapterNumber - 1]
                let expectedPages = (1...max(entry.pageCount, 1)).map { String(format: "%04d.jpg", $0) }
                guard entry.pageCount > 0, !entry.title.isEmpty,
                      imageURLs.map(\.lastPathComponent) == expectedPages else {
                    throw GeneratorError.catalogMismatch("\(subject.slug)/\(entry.folderName)")
                }

                let outputURL = subjectOutput.appendingPathComponent(
                    String(format: "%02d.pdf", chapterNumber)
                )

                if needsRegeneration(outputURL: outputURL, sourceImages: imageURLs) {
                    try generatePDF(
                        from: imageURLs,
                        title: entry.title,
                        subject: subject.title,
                        outputURL: outputURL
                    )
                    print("Generated \(subject.slug)/\(outputURL.lastPathComponent) — \(imageURLs.count) pages")
                }

                subjectPages += imageURLs.count
            }

            totalChapters += chapterFolders.count
            totalPages += subjectPages
            manifestSubjects.append([
                "slug": subject.slug,
                "chapters": chapterFolders.count,
                "pages": subjectPages,
            ])
        }

        try catalogData.write(to: outputRoot.appendingPathComponent("catalog.json"), options: .atomic)

        let manifest: [String: Any] = [
            "formatVersion": 1,
            "chapterCount": totalChapters,
            "pageCount": totalPages,
            "subjects": manifestSubjects,
        ]
        let manifestData = try JSONSerialization.data(
            withJSONObject: manifest,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        )
        try manifestData.write(
            to: outputRoot.appendingPathComponent("manifest.json"),
            options: .atomic
        )

        print("Offline archive ready — \(totalChapters) chapters, \(totalPages) pages")
    }

    private static func chapterNumber(for folderURL: URL) -> Int {
        Int(folderURL.lastPathComponent.split(separator: ".", maxSplits: 1).first ?? "") ?? .max
    }

    private static func chapterTitle(for folderURL: URL) -> String {
        let components = folderURL.lastPathComponent.split(separator: ".", maxSplits: 1)
        return components.count == 2 ? String(components[1]) : folderURL.lastPathComponent
    }

    private static func needsRegeneration(outputURL: URL, sourceImages: [URL]) -> Bool {
        guard
            let document = PDFDocument(url: outputURL),
            document.pageCount == sourceImages.count,
            let outputDate = try? outputURL.resourceValues(
                forKeys: [.contentModificationDateKey]
            ).contentModificationDate
        else {
            return true
        }

        return sourceImages.contains { imageURL in
            guard let sourceDate = try? imageURL.resourceValues(
                forKeys: [.contentModificationDateKey]
            ).contentModificationDate else {
                return true
            }
            return sourceDate > outputDate
        }
    }

    private static func generatePDF(
        from imageURLs: [URL],
        title: String,
        subject: String,
        outputURL: URL
    ) throws {
        let document = PDFDocument()
        document.documentAttributes = [
            PDFDocumentAttribute.titleAttribute: title,
            PDFDocumentAttribute.subjectAttribute: "\(subject) handwritten revision notes",
            PDFDocumentAttribute.creatorAttribute: "PCM Notes",
        ]

        for imageURL in imageURLs {
            try autoreleasepool {
                guard let image = NSImage(contentsOf: imageURL) else {
                    throw GeneratorError.unreadableImage(imageURL)
                }
                guard let page = PDFPage(image: image) else {
                    throw GeneratorError.unwritablePage(imageURL)
                }
                document.insert(page, at: document.pageCount)
            }
        }

        let temporaryURL = outputURL.appendingPathExtension("tmp")
        try? FileManager.default.removeItem(at: temporaryURL)

        guard document.write(to: temporaryURL) else {
            throw GeneratorError.unwritableDocument(outputURL)
        }

        if FileManager.default.fileExists(atPath: outputURL.path) {
            try FileManager.default.removeItem(at: outputURL)
        }
        try FileManager.default.moveItem(at: temporaryURL, to: outputURL)
    }
}
