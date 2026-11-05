import UIKit
import XCTest

/// The iPad-only reading layout. scripts/test-ios.sh runs this class on an iPad
/// simulator and every other UI test on an iPhone, so each runs exactly once.
final class iPadReaderUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
        guard UIDevice.current.userInterfaceIdiom == .pad else {
            throw XCTSkip("This suite runs on the wide iPad display in CI.")
        }
    }

    /// Switches to the two-page spread, pages through it in pairs, and finds both
    /// the layout and the reading position kept when the chapter is reopened.
    @MainActor
    func testIPadTwoPageLayoutIsAvailable() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()

        app.openChapter("physics", 1, expecting: "Page 1 of 9")

        let layoutMenu = app.buttons["reader.layoutMenu"]
        XCTAssertTrue(layoutMenu.waitForExistence(timeout: stepTimeout))
        XCTAssertEqual(layoutMenu.value as? String, "Continuous")

        layoutMenu.tap()

        let twoPageOption = app.descendants(matching: .any)["Two-page spread"]
        XCTAssertTrue(twoPageOption.waitForExistence(timeout: stepTimeout))
        twoPageOption.tap()

        let layoutChanged = NSPredicate(
            format: "value == %@",
            "Two-page spread"
        )
        expectation(for: layoutChanged, evaluatedWith: layoutMenu)
        waitForExpectations(timeout: stepTimeout)

        // Page 1 stands alone like a cover, then pages pair up.
        let slider = app.sliders["reader.pageSlider"]
        let next = app.buttons["Next page"]
        XCTAssertTrue(slider.waitForValue("Page 1 of 9"))
        next.tap()
        XCTAssertTrue(slider.waitForValue("Pages 2 through 3 of 9"))
        XCTAssertTrue(app.staticTexts["2–3 / 9"].exists)
        next.tap()
        XCTAssertTrue(slider.waitForValue("Pages 4 through 5 of 9"))

        app.buttons["Close focus reader"].tap()
        XCTAssertTrue(app.chapter("physics", 1).waitForExistence(timeout: stepTimeout))

        app.openChapter("physics", 1, expecting: "Pages 4 through 5 of 9")
        XCTAssertTrue(layoutMenu.waitForValue("Two-page spread"))
    }

    /// The sidebar opens another bundled chapter without leaving the reader.
    @MainActor
    func testChapterSidebarKeepsIndependentReadingProgress() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()
        app.openChapter("physics", 1, expecting: "Page 1 of 9")
        app.buttons["Next page"].tap()
        XCTAssertTrue(app.sliders["reader.pageSlider"].waitForValue("Page 2 of 9"))

        let second = app.buttons["reader.chapter.physics.2"]
        if !second.isHittable {
            let toggle = app.buttons["Toggle Sidebar"]
            XCTAssertTrue(toggle.waitForExistence(timeout: stepTimeout))
            toggle.tap()
        }
        XCTAssertTrue(second.waitForExistence(timeout: stepTimeout))
        second.tap()
        XCTAssertTrue(app.sliders["reader.pageSlider"].waitForValue("Page 1 of 14"))
        app.buttons["Close focus reader"].tap()
        app.openChapter("physics", 1, expecting: "Page 2 of 9")
    }
}
