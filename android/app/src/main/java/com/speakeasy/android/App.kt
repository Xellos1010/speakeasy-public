package com.speakeasy.android

import android.app.Application

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        SpeakEasy.createChannel(this)
        val se = SpeakEasy.get(this)
        if (se.queue.size() > 0) SpeakEasy.scheduleDrain(this)
    }
}
