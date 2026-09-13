package ai.elren.mobile

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread
import android.util.Base64
import java.io.ByteArrayOutputStream
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit

class ScreenCaptureController(
    private val context: Context,
    private val onStateChanged: (Boolean) -> Unit = {},
) {
    private val thread = HandlerThread("ElrenScreenCapture").apply { start() }
    private val handler = Handler(thread.looper)
    private val resourceLock = Any()
    private val captureLock = Any()
    @Volatile private var projection: MediaProjection? = null
    @Volatile private var reader: ImageReader? = null
    @Volatile private var display: VirtualDisplay? = null
    @Volatile private var pendingCapture: CompletableFuture<ByteArray>? = null
    @Volatile private var captureWidth = 0
    @Volatile private var captureHeight = 0

    fun setProjection(resultCode: Int, data: Intent) {
        // Replacing an existing projection is one logical transition. Do not
        // briefly publish a false state between the old and new sessions.
        releaseProjection(stopProjection = true, notifyStateChange = false)
        val manager = context.getSystemService(MediaProjectionManager::class.java)
        val activeProjection = manager.getMediaProjection(resultCode, data)
        val metrics = context.resources.displayMetrics
        val width = metrics.widthPixels
        val height = metrics.heightPixels
        val activeReader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3)
        activeReader.setOnImageAvailableListener({ source -> consumeFrame(source) }, handler)
        synchronized(resourceLock) {
            // Publish the new session before registering its callback. An old
            // MediaProjection callback may still be queued after replacement;
            // releaseProjection's identity guard prevents that stale callback
            // from tearing down this new session.
            projection = activeProjection
            reader = activeReader
            captureWidth = width
            captureHeight = height
        }
        try {
            activeProjection.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    releaseProjection(stopProjection = false, expectedProjection = activeProjection)
                }
            }, handler)
            val activeDisplay = activeProjection.createVirtualDisplay(
                "ElrenCapture",
                width,
                height,
                metrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                activeReader.surface,
                null,
                handler,
            )
            synchronized(resourceLock) {
                if (projection !== activeProjection) {
                    activeDisplay.release()
                    error("SCREEN_CAPTURE_PERMISSION_REQUIRED: screen capture permission ended")
                }
                display = activeDisplay
            }
            onStateChanged(true)
        } catch (error: Throwable) {
            releaseProjection(stopProjection = true, expectedProjection = activeProjection)
            throw error
        }
    }

    fun captureBase64(): String {
        val bytes = synchronized(captureLock) {
            val activeReader = synchronized(resourceLock) {
                if (projection == null || display == null) {
                    error("SCREEN_CAPTURE_PERMISSION_REQUIRED: allow screen capture again on the phone")
                }
                reader ?: error("SCREEN_CAPTURE_PERMISSION_REQUIRED: screen capture session is unavailable")
            }
            val future = CompletableFuture<ByteArray>()
            pendingCapture = future
            // A frame may already be queued while the screen is static. Ask
            // the capture thread to consume it instead of waiting for a new
            // display update that may never arrive.
            handler.post { consumeFrame(activeReader) }
            try {
                future.get(8, TimeUnit.SECONDS)
            } finally {
                if (pendingCapture === future) pendingCapture = null
            }
        }
        require(bytes.size <= 12 * 1024 * 1024) { "Screenshot exceeds 12 MiB" }
        return Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    private fun consumeFrame(source: ImageReader) {
        val future = pendingCapture ?: return
        if (future.isDone || source !== reader) return
        val image = try { source.acquireLatestImage() } catch (error: Throwable) {
            future.completeExceptionally(error)
            return
        } ?: return
        try {
            val width = captureWidth
            val height = captureHeight
            check(width > 0 && height > 0) { "Screen capture dimensions are unavailable" }
            val plane = image.planes[0]
            val rowPadding = plane.rowStride - plane.pixelStride * width
            val paddedWidth = width + rowPadding / plane.pixelStride
            val padded = Bitmap.createBitmap(paddedWidth, height, Bitmap.Config.ARGB_8888)
            padded.copyPixelsFromBuffer(plane.buffer)
            val cropped = if (paddedWidth == width) padded else {
                Bitmap.createBitmap(padded, 0, 0, width, height)
            }
            try {
                val output = ByteArrayOutputStream()
                cropped.compress(Bitmap.CompressFormat.PNG, 100, output)
                future.complete(output.toByteArray())
            } finally {
                if (cropped !== padded) cropped.recycle()
                padded.recycle()
            }
        } catch (error: Throwable) {
            future.completeExceptionally(error)
        } finally {
            image.close()
        }
    }

    private fun releaseProjection(
        stopProjection: Boolean,
        expectedProjection: MediaProjection? = null,
        notifyStateChange: Boolean = true,
    ) {
        val activeProjection: MediaProjection?
        synchronized(resourceLock) {
            if (expectedProjection != null && projection !== expectedProjection) return
            pendingCapture?.completeExceptionally(
                IllegalStateException("SCREEN_CAPTURE_PERMISSION_REQUIRED: screen capture permission ended"),
            )
            pendingCapture = null
            display?.release()
            display = null
            reader?.close()
            reader = null
            activeProjection = projection
            projection = null
            captureWidth = 0
            captureHeight = 0
        }
        if (notifyStateChange) onStateChanged(false)
        if (stopProjection) activeProjection?.stop()
    }

    fun close() {
        releaseProjection(stopProjection = true)
        thread.quitSafely()
    }
}
