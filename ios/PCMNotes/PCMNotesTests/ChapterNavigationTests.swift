import Testing
@testable import PCMNotes

/// Chapter search and reading progress, as the subject lists use them.
@MainActor
struct ChapterNavigationTests {
    @Test func emptySearchListsEveryChapterInOrder() {
        for subject in SubjectArea.allCases {
            #expect(subject.chapters(matching: "") == subject.chapters)
        }
    }

    @Test func searchMatchesTitlesIgnoringCase() {
        #expect(SubjectArea.physics.chapters(matching: "kinematics").map(\.number) == [3])
        #expect(SubjectArea.chemistry.chapters(matching: "BLOCK").map(\.number) == [11, 12, 28])
        #expect(SubjectArea.mathematics.chapters(matching: "Integration").map(\.number) == [26, 27])
        #expect(SubjectArea.physics.chapters(matching: "newton's").map(\.title) == ["Newton's Laws of Motion"])
    }

    @Test func searchMatchesChapterNumbers() {
        #expect(SubjectArea.physics.chapters(matching: "19").map(\.number) == [19])
        // "3" matches chapters 3, 13, 23, 30 and 31 (whose title also says 3D).
        #expect(SubjectArea.mathematics.chapters(matching: "3").map(\.number) == [3, 13, 23, 30, 31])
    }

    @Test func unmatchedSearchFindsNothing() {
        #expect(SubjectArea.physics.chapters(matching: "Biology").isEmpty)
        #expect(SubjectArea.chemistry.chapters(matching: "99").isEmpty)
    }

    @Test func resumePageStaysWithinTheChapter() {
        let vectors = ChapterCatalog.physics[0]
        #expect(vectors.pageCount == 9)
        #expect(vectors.resumePage(fromSaved: 0) == 1, "an unread chapter opens at page 1")
        #expect(vectors.resumePage(fromSaved: -4) == 1)
        #expect(vectors.resumePage(fromSaved: 4) == 4)
        #expect(vectors.resumePage(fromSaved: 9) == 9)
        #expect(vectors.resumePage(fromSaved: 42) == 9, "a stale page never opens past the end")
    }

    @Test func progressSummaryOffersToResumeOnlyAfterTheFirstPage() {
        let vectors = ChapterCatalog.physics[0]
        #expect(vectors.progressSummary(savedPage: 0) == "9 pages · offline PDF")
        #expect(vectors.progressSummary(savedPage: 1) == "9 pages · offline PDF")
        #expect(vectors.progressSummary(savedPage: 4) == "Resume page 4 of 9")
        #expect(vectors.progressSummary(savedPage: 42) == "Resume page 9 of 9")
        #expect(vectors.progressSummary(savedPage: -1) == "9 pages · offline PDF")
    }

    @Test func everyChapterKeepsItsOwnReadingProgress() {
        let keys = SubjectArea.allCases.flatMap { subject in
            subject.chapters.map { $0.readingProgressKey(for: subject) }
        }
        #expect(keys.count == ChapterCatalog.totalChapterCount)
        #expect(Set(keys).count == keys.count)
        #expect(ChapterCatalog.mathematics[30].readingProgressKey(for: .mathematics) == "reader.mathematics.31.page")
    }

    @Test func subjectTabsUseShortNamesOnlyForMaths() {
        #expect(SubjectArea.allCases.map(\.tabName) == ["Physics", "Chemistry", "Maths"])
        #expect(SubjectArea.allCases.map(\.name) == ["Physics", "Chemistry", "Mathematics"])
        #expect(Set(SubjectArea.allCases.map(\.slug)).count == 3)
    }
}
