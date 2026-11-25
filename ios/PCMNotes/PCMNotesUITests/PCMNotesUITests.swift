//
//  PCMNotesUITests.swift
//  PCMNotesUITests
//
//  Created by Apoorv Darshan on 20/07/26.
//

import XCTest

/// The main journeys on iPhone: tabs and subject indexes, search, the offline reader
/// and its saved progress, appearance, support with the tip jar, and About.
/// Each test starts from a fresh launch (see `XCUIApplication.pcmNotes`) and covers
/// one journey end to end, so CI launches the app as few times as possible.
final class PCMNotesUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    // MARK: - Navigation

    @MainActor
    func testFivePrimaryTabsAreReachable() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()

        let tabBar = app.tabBars.firstMatch
        XCTAssertTrue(tabBar.waitForExistence(timeout: launchTimeout))

        // Each subject tab opens its own chapter index.
        let subjects: [(tab: String, slug: String, title: String, chapters: Int, firstChapter: String, pages: String)] = [
            ("Physics", "physics", "Physics", 23, "Chapter 1, Vectors", "9 pages · offline PDF"),
            ("Chemistry", "chemistry", "Chemistry", 37, "Chapter 1, Some Basic Concepts of Chemistry", "81 pages · offline PDF"),
            ("Maths", "mathematics", "Mathematics", 31, "Chapter 1, Set, Relation & Function", "58 pages · offline PDF"),
        ]
        for subject in subjects {
            let tab = tabBar.buttons[subject.tab]
            XCTAssertTrue(tab.exists, "Missing \(subject.tab) tab")
            tab.tap()
            let firstRow = app.chapter(subject.slug, 1)
            XCTAssertTrue(firstRow.waitForExistence(timeout: launchTimeout), "\(subject.tab) has no chapter list")
            XCTAssertEqual(firstRow.label, subject.firstChapter)
            XCTAssertEqual(firstRow.value as? String, subject.pages)
            XCTAssertTrue(app.staticTexts[subject.title].exists, "Missing \(subject.title) heading")
            XCTAssertTrue(
                app.element(labeled: "Chapter index, \(subject.chapters) total").exists,
                "\(subject.tab) should list \(subject.chapters) chapters"
            )
        }

        for tabName in ["Support", "About"] {
            let tab = tabBar.buttons[tabName]
            XCTAssertTrue(tab.exists, "Missing \(tabName) tab")
            tab.tap()
            XCTAssertTrue(app.navigationBars[tabName].waitForExistence(timeout: stepTimeout))
        }
    }

    @MainActor
    func testTabBarStaysExpandedWhileScrolling() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()

        let tabBar = app.tabBars.firstMatch
        XCTAssertTrue(tabBar.waitForExistence(timeout: launchTimeout))
        XCTAssertTrue(app.chapter("physics", 1).waitForExistence(timeout: launchTimeout))

        app.swipeUp()
        app.swipeUp()
        app.swipeUp()

        for tabName in ["Physics", "Chemistry", "Maths", "Support", "About"] {
            let tab = tabBar.buttons[tabName]
            XCTAssertTrue(tab.exists, "Missing \(tabName) tab after scrolling")
            XCTAssertTrue(tab.isHittable, "Collapsed \(tabName) tab after scrolling")
        }
    }

    @MainActor
    func testChapterSearchFiltersByTitleAndNumber() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()
        XCTAssertTrue(app.chapter("physics", 1).waitForExistence(timeout: launchTimeout))

        let search = app.searchFields.firstMatch
        if !search.waitForExistence(timeout: stepTimeout) {
            app.swipeDown()  // the search bar can sit collapsed under the navigation bar
        }
        XCTAssertTrue(search.waitForExistence(timeout: stepTimeout))
        search.tap()

        search.typeText("kinematics")
        XCTAssertTrue(app.element(labeled: "Chapter index, 1 shown").waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(app.chapter("physics", 3).exists)
        XCTAssertFalse(app.chapter("physics", 1).exists)

        search.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: "kinematics".count))
        search.typeText("19")
        XCTAssertTrue(app.element(labeled: "Chapter index, 1 shown").waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(app.chapter("physics", 19).waitForExistence(timeout: stepTimeout))
        XCTAssertEqual(app.chapter("physics", 19).label, "Chapter 19, Optics")

        search.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 2))
        search.typeText("biology")
        XCTAssertTrue(app.element(labeled: "Chapter index, 0 shown").waitForExistence(timeout: stepTimeout))
        XCTAssertFalse(app.chapter("physics", 19).exists)

        search.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: "biology".count))
        XCTAssertTrue(app.element(labeled: "Chapter index, 23 total").waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(app.chapter("physics", 1).exists)
    }

    // MARK: - Reader

    /// Opens a chapter, pages through it, closes straight back to the list, and
    /// resumes at the saved page after a relaunch.
    @MainActor
    func testOfflinePDFReaderClosesDirectlyToChapterList() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()

        let firstChapter = app.chapter("physics", 1)
        let closeReader = app.buttons["Close focus reader"]
        let pageSlider = app.sliders["reader.pageSlider"]
        let previous = app.buttons["Previous page"]
        let next = app.buttons["Next page"]

        XCTAssertTrue(firstChapter.waitForExistence(timeout: launchTimeout))
        XCTAssertEqual(firstChapter.value as? String, "9 pages · offline PDF")

        app.openChapter("physics", 1, expecting: "Page 1 of 9")
        XCTAssertTrue(closeReader.exists)
        XCTAssertFalse(previous.isEnabled, "page 1 has nothing before it")

        next.tap()
        XCTAssertTrue(pageSlider.waitForValue("Page 2 of 9"))
        next.tap()
        XCTAssertTrue(pageSlider.waitForValue("Page 3 of 9"))
        XCTAssertTrue(app.staticTexts["3 / 9"].exists)
        previous.tap()
        XCTAssertTrue(pageSlider.waitForValue("Page 2 of 9"))

        // XCUITest can't place this slider on an exact page: its accessibility value is
        // "Page 2 of 9", not a percentage, and on GitHub's macOS runners a drag to the
        // end sometimes stopped a page short. So check the slider jumps far ahead, then
        // reach the last page with Next page.
        pageSlider.adjust(toNormalizedSliderPosition: 1)
        let jumped = NSPredicate { slider, _ in
            guard let value = (slider as? XCUIElement)?.value as? String,
                  let page = Int(value.split(separator: " ")[1]) else { return false }
            return page >= 7
        }
        XCTAssertEqual(
            XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: jumped, object: pageSlider)], timeout: stepTimeout),
            .completed,
            "the slider should jump near the end, not \(String(describing: pageSlider.value))"
        )
        for _ in 0..<8 where (pageSlider.value as? String) != "Page 9 of 9" {
            next.tap()
        }
        XCTAssertTrue(pageSlider.waitForValue("Page 9 of 9"))
        XCTAssertFalse(next.isEnabled, "the last page has nothing after it")
        previous.tap()
        XCTAssertTrue(pageSlider.waitForValue("Page 8 of 9"))

        closeReader.tap()

        XCTAssertTrue(closeReader.waitForNonExistence(timeout: stepTimeout))
        XCTAssertTrue(pageSlider.waitForNonExistence(timeout: stepTimeout))
        XCTAssertTrue(firstChapter.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(firstChapter.isHittable)
        XCTAssertFalse(app.buttons["Open focus reader"].exists)
        XCTAssertTrue(firstChapter.waitForValue("Resume page 8 of 9"))

        app.relaunchKeepingData()
        XCTAssertTrue(firstChapter.waitForExistence(timeout: launchTimeout))
        XCTAssertTrue(firstChapter.waitForValue("Resume page 8 of 9"), "progress must survive a relaunch")
        app.openChapter("physics", 1, expecting: "Page 8 of 9")
    }

    // MARK: - Appearance

    @MainActor
    func testAppearanceSelectorOffersSystemLightAndDark() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()

        app.openTab("About")

        let systemChoice = app.buttons["appearance.system"]
        let lightChoice = app.buttons["appearance.light"]
        let darkChoice = app.buttons["appearance.dark"]

        XCTAssertTrue(darkChoice.waitForExistence(timeout: stepTimeout))
        app.reveal(darkChoice)

        XCTAssertTrue(systemChoice.isHittable)
        XCTAssertTrue(lightChoice.isHittable)
        XCTAssertTrue(darkChoice.isHittable)
        XCTAssertEqual(systemChoice.value as? String, "Selected", "System is the default")

        lightChoice.tap()
        XCTAssertEqual(lightChoice.value as? String, "Selected")

        systemChoice.tap()
        XCTAssertEqual(systemChoice.value as? String, "Selected")

        darkChoice.tap()
        XCTAssertEqual(darkChoice.value as? String, "Selected")

        // The choice is kept across launches.
        app.relaunchKeepingData()
        app.openTab("About")
        XCTAssertTrue(darkChoice.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(darkChoice.waitForValue("Selected"), "Dark must still be chosen after a relaunch")
        XCTAssertEqual(systemChoice.value as? String, "Not selected")
    }

    // MARK: - Support and tips (local tip store: nothing is charged)

    @MainActor
    func testSupportShowsContactsAndOptionalTips() throws {
        let app = XCUIApplication.pcmNotes(tipOutcome: .purchased)
        app.launch()

        app.openTab("Support")

        let email = app.descendants(matching: .any)["support.email"]
        let xProfile = app.descendants(matching: .any)["support.x"]
        let instagramProfile = app.descendants(matching: .any)["support.instagram"]
        let rate = app.descendants(matching: .any)["support.rate"]
        let bugReport = app.descendants(matching: .any)["support.bug"]
        let featureRequest = app.descendants(matching: .any)["support.feature"]
        let sourceCode = app.descendants(matching: .any)["support.github"]
        let share = app.descendants(matching: .any)["support.share"]

        XCTAssertTrue(email.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(xProfile.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(instagramProfile.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(rate.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(bugReport.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(featureRequest.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(sourceCode.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(share.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(app.staticTexts["ad13dtu@gmail.com"].exists)
        XCTAssertTrue(app.staticTexts["@apoorvdarshan"].exists)
        XCTAssertTrue(app.staticTexts["@apoorvxi"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["tip.disclosure"].waitForExistence(timeout: stepTimeout))

        let prices = ["small": "Small Tip, ₹49", "medium": "Chai Tip, ₹99",
                      "large": "Study Fuel, ₹199", "generous": "Notebook Hero, ₹499"]
        for tier in ["small", "medium", "large", "generous"] {
            let tipButton = app.buttons["tip.product.\(tier)"]
            XCTAssertTrue(
                tipButton.waitForExistence(timeout: stepTimeout),
                "Missing repeatable \(tier) tip"
            )
            XCTAssertTrue(tipButton.waitForLabel(prices[tier]!), "\(tier) shows \(tipButton.label)")
            XCTAssertTrue(tipButton.isEnabled)
        }

        // Buying a tip thanks the reader; the note can be dismissed and tips stay repeatable.
        let chaiTip = app.buttons["tip.product.medium"]
        app.reveal(chaiTip)
        chaiTip.tap()

        let thanks = app.staticTexts["Thank you—your tip helps keep PCM Notes free, ad-free, and improving."]
        XCTAssertTrue(thanks.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(chaiTip.isEnabled, "tips are repeatable")

        app.buttons["Dismiss tip status"].tap()
        XCTAssertTrue(thanks.waitForNonExistence(timeout: stepTimeout))
    }

    @MainActor
    func testTipJarRecoversWhenPricesAreUnavailable() throws {
        let app = XCUIApplication.pcmNotes(tips: .unavailableThenReady)
        app.launch()

        app.openTab("Support")
        let smallTip = app.buttons["tip.product.small"]
        XCTAssertTrue(smallTip.waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(smallTip.waitForLabel("Small Tip, unavailable"))
        XCTAssertFalse(smallTip.isEnabled, "a tip without a price cannot be bought")
        XCTAssertTrue(app.staticTexts["Tip prices are unavailable right now."].exists)

        let retry = app.buttons["tip.retry"]
        app.reveal(retry)
        retry.tap()

        XCTAssertTrue(smallTip.waitForLabel("Small Tip, ₹49"))
        XCTAssertTrue(smallTip.isEnabled)
        XCTAssertTrue(app.staticTexts["Tip prices are unavailable right now."].waitForNonExistence(timeout: stepTimeout))
    }

    // MARK: - About

    @MainActor
    func testAboutShowsInAppLegalPages() throws {
        let app = XCUIApplication.pcmNotes()
        app.launch()

        app.openTab("About")
        XCTAssertTrue(app.navigationBars["About"].waitForExistence(timeout: stepTimeout))

        // The archive summary comes from the chapter catalog.
        for summary in ["23 chapters · 539 pages", "37 chapters · 993 pages", "31 chapters · 641 pages"] {
            let row = app.descendants(matching: .any)
                .matching(NSPredicate(format: "label CONTAINS %@", summary)).firstMatch
            XCTAssertTrue(row.waitForExistence(timeout: stepTimeout), "About is missing \(summary)")
        }

        let privacyLink = app.descendants(matching: .any)["legal.privacy"]
        XCTAssertTrue(privacyLink.waitForExistence(timeout: stepTimeout))
        app.reveal(privacyLink)
        privacyLink.tap()

        XCTAssertTrue(app.navigationBars["Privacy Policy"].waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(app.staticTexts["NO TRACKING"].exists)
        XCTAssertTrue(app.staticTexts["What the app stores"].exists)

        app.navigationBars.buttons["About"].tap()

        let termsLink = app.descendants(matching: .any)["legal.terms"]
        XCTAssertTrue(termsLink.waitForExistence(timeout: stepTimeout))
        app.reveal(termsLink)
        termsLink.tap()

        XCTAssertTrue(app.navigationBars["Terms of Use"].waitForExistence(timeout: stepTimeout))
        XCTAssertTrue(app.staticTexts["PERSONAL STUDY"].exists)
        XCTAssertTrue(app.staticTexts["Personal study use"].exists)

        app.navigationBars.buttons["About"].tap()
        let version = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'Version '")).firstMatch
        XCTAssertTrue(version.waitForExistence(timeout: stepTimeout))
        app.reveal(version)
        XCTAssertNotNil(
            version.label.range(of: #"^Version \d+(\.\d+)* \(\d+\)$"#, options: .regularExpression),
            "unexpected version text \(version.label)"
        )
    }
}
