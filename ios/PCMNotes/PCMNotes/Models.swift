import Foundation
import SwiftUI

struct Chapter: Codable, Identifiable, Hashable, Sendable {
    let number: Int
    let title: String
    let folderName: String
    let pageCount: Int

    var id: String { folderName }

    var pdfResourceName: String {
        String(format: "%02d", number)
    }

    func pdfURL(for subject: SubjectArea, in bundle: Bundle = .main) -> URL? {
        bundle.url(
            forResource: pdfResourceName,
            withExtension: "pdf",
            subdirectory: "OfflineNotes/\(subject.slug)"
        )
    }

    func readingProgressKey(for subject: SubjectArea) -> String {
        "reader.\(subject.slug).\(number).page"
    }

    /// The page the reader opens at: the saved page, or page 1 for a chapter that
    /// was never opened, never past the chapter's last page.
    func resumePage(fromSaved savedPage: Int) -> Int {
        min(max(savedPage, 1), pageCount)
    }

    /// The chapter row's progress line: where to resume, or the size of an unread chapter.
    func progressSummary(savedPage: Int) -> String {
        if savedPage > 1 {
            "Resume page \(savedPage) of \(pageCount)"
        } else {
            "\(pageCount) pages · offline PDF"
        }
    }
}

enum SubjectArea: String, CaseIterable, Identifiable, Hashable, Sendable {
    case physics
    case chemistry
    case mathematics

    var id: String { rawValue }
    var slug: String { rawValue }

    var name: String {
        switch self {
        case .physics: "Physics"
        case .chemistry: "Chemistry"
        case .mathematics: "Mathematics"
        }
    }

    var tabName: String {
        self == .mathematics ? "Maths" : name
    }

    var symbol: String {
        switch self {
        case .physics: "atom"
        case .chemistry: "flask.fill"
        case .mathematics: "function"
        }
    }

    var accent: Color {
        switch self {
        case .physics: AppPalette.physics
        case .chemistry: AppPalette.chemistry
        case .mathematics: AppPalette.mathematics
        }
    }

    var secondaryAccent: Color {
        switch self {
        case .physics: AppPalette.physicsSecondary
        case .chemistry: AppPalette.chemistrySecondary
        case .mathematics: AppPalette.mathematicsSecondary
        }
    }

    var tagline: String {
        switch self {
        case .physics: "Build intuition from vectors to modern physics."
        case .chemistry: "Revise physical, organic, and inorganic chemistry."
        case .mathematics: "Move from fundamentals to calculus and 3D geometry."
        }
    }

    var chapters: [Chapter] {
        switch self {
        case .physics: ChapterCatalog.physics
        case .chemistry: ChapterCatalog.chemistry
        case .mathematics: ChapterCatalog.mathematics
        }
    }

    var pageCount: Int {
        chapters.reduce(0) { $0 + $1.pageCount }
    }

    /// Chapters whose title or number contains `query`, ignoring case; every
    /// chapter for an empty query.
    func chapters(matching query: String) -> [Chapter] {
        guard !query.isEmpty else { return chapters }

        return chapters.filter { chapter in
            chapter.title.localizedCaseInsensitiveContains(query)
                || String(chapter.number).localizedCaseInsensitiveContains(query)
        }
    }
}

enum ChapterCatalog {
    // The same catalog drives the public website and the offline PDF build.
    private static let catalog: [String: [Chapter]] = {
        guard let url = Bundle.main.url(
            forResource: "catalog", withExtension: "json", subdirectory: "OfflineNotes"
        ), let data = try? Data(contentsOf: url),
           let chapters = try? JSONDecoder().decode([String: [Chapter]].self, from: data),
           Set(chapters.keys) == Set(SubjectArea.allCases.map(\.slug)) else {
            preconditionFailure("The validated offline chapter catalog was not bundled.")
        }
        return chapters
    }()

    static let physics = catalog["physics"] ?? []
    static let chemistry = catalog["chemistry"] ?? []
    static let mathematics = catalog["mathematics"] ?? []

    static let totalChapterCount = physics.count + chemistry.count + mathematics.count
    static let totalPageCount = [physics, chemistry, mathematics]
        .flatMap { $0 }
        .reduce(0) { $0 + $1.pageCount }
}
