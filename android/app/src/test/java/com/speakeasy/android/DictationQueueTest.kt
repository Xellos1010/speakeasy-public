package com.speakeasy.android

import com.speakeasy.android.queue.DictationQueue
import com.speakeasy.android.queue.SendOutcome
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class DictationQueueTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test fun persistsAcrossInstancesInFifoOrder() {
        val dir = tmp.newFolder("q")
        val q = DictationQueue(dir)
        q.enqueue(byteArrayOf(1), "raw", id = "b", now = 2)
        q.enqueue(byteArrayOf(2), "email", appHint = "com.whatsapp", id = "a", now = 1)
        val q2 = DictationQueue(dir) // simulate process restart
        assertEquals(listOf("a", "b"), q2.list().map { it.id })
        assertEquals("com.whatsapp", q2.list().first().appHint)
        assertArrayEquals(byteArrayOf(2), q2.audio("a").readBytes())
    }

    @Test fun retryStopsDrainAndKeepsAudio() {
        val q = DictationQueue(tmp.newFolder("q"))
        q.enqueue(byteArrayOf(1), "raw", id = "1", now = 1)
        q.enqueue(byteArrayOf(2), "raw", id = "2", now = 2)
        var calls = 0
        val (delivered, remaining) = q.drain { _, _ -> calls++; SendOutcome.Retry("offline") }
        assertEquals(0, delivered); assertEquals(2, remaining); assertEquals(1, calls)
        assertEquals(1, q.list().first().attempts); assertEquals("offline", q.list().first().lastError)
    }

    @Test fun deliveredRemovedRejectedKept() {
        val q = DictationQueue(tmp.newFolder("q"))
        q.enqueue(byteArrayOf(1), "raw", id = "ok", now = 1)
        q.enqueue(byteArrayOf(2), "raw", id = "bad", now = 2)
        q.enqueue(byteArrayOf(3), "raw", id = "ok2", now = 3)
        val got = mutableListOf<String>()
        val (delivered, remaining) = q.drain({ item, _ -> got += item.id }) { item, _ ->
            if (item.id == "bad") SendOutcome.Rejected("HTTP 400") else SendOutcome.Delivered(item.id)
        }
        assertEquals(2, delivered); assertEquals(1, remaining)
        assertEquals(listOf("ok", "ok2"), got)
        assertEquals("bad", q.list().single().id)
        assertEquals(true, q.audio("bad").exists())
    }

    @Test fun exceptionInSenderIsTreatedAsRetry() {
        val q = DictationQueue(tmp.newFolder("q"))
        q.enqueue(byteArrayOf(1), "raw", id = "x")
        val (_, remaining) = q.drain { _, _ -> throw java.io.IOException("boom") }
        assertEquals(1, remaining); assertEquals("boom", q.list().single().lastError)
    }
}
