package com.speakeasy.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

/** Audit P0: dictation history, queued raw audio and the device token must never be backed up. */
class BackupPolicyTest {
    private val res = File("src/main")
    private fun parse(f: File) = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
        .newDocumentBuilder().parse(f).documentElement
    private val domains = setOf("root", "file", "database", "sharedpref", "external")
    private fun excluded(e: Element): Set<String> {
        val nl = e.getElementsByTagName("exclude")
        return (0 until nl.length).map { nl.item(it) as Element }.filter { it.getAttribute("path") == "." }
            .map { it.getAttribute("domain") }.toSet()
    }
    private val android = "http://schemas.android.com/apk/res/android"

    @Test fun manifestDisablesBackup() {
        val app = parse(File(res, "AndroidManifest.xml")).getElementsByTagName("application").item(0) as Element
        assertEquals("false", app.getAttributeNS(android, "allowBackup"))
        assertEquals("@xml/backup_rules", app.getAttributeNS(android, "fullBackupContent"))
        assertEquals("@xml/data_extraction_rules", app.getAttributeNS(android, "dataExtractionRules"))
    }

    @Test fun legacyRulesExcludeEverything() {
        val root = parse(File(res, "res/xml/backup_rules.xml"))
        assertEquals(0, root.getElementsByTagName("include").length)
        assertTrue(excluded(root).containsAll(domains))
    }

    @Test fun cloudAndDeviceTransferExcludeEverything() {
        val root = parse(File(res, "res/xml/data_extraction_rules.xml"))
        assertEquals(0, root.getElementsByTagName("include").length)
        for (tag in listOf("cloud-backup", "device-transfer")) {
            val sec = root.getElementsByTagName(tag).item(0) as Element
            assertTrue("$tag must exclude all domains", excluded(sec).containsAll(domains))
        }
    }
}
