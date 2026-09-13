package org.elren.omr;

import java.io.File;
import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.nio.file.Path;
import javax.swing.filechooser.FileSystemView;

/** Task-local path policy for Audiveris's private, headless JVM only. */
public final class ElrenAudiverisBootstrap {
    private ElrenAudiverisBootstrap() { }

    public static void configureTaskDirectories() throws Exception {
        if (!"true".equals(System.getProperty("java.awt.headless"))) {
            throw new IllegalStateException("Audiveris bootstrap requires a headless task JVM");
        }
        String configured = System.getProperty("elren.omr.state.root");
        if (configured == null || configured.isBlank()) {
            throw new IllegalStateException("Missing task-local Audiveris state root");
        }
        Path state = Path.of(configured).toRealPath();
        if (!state.equals(Path.of(System.getProperty("user.home")).toRealPath())) {
            throw new IllegalStateException("Audiveris user.home must be the task state root");
        }
        Path documents = state.resolve("Documents").toRealPath();
        if (!documents.startsWith(state) || documents.equals(state)) {
            throw new IllegalStateException("Audiveris Documents must remain inside task state");
        }
        if (File.separatorChar == '\\') {
            // Audiveris 5.11.0 calls this singleton, whose Windows default otherwise
            // resolves the real KnownFolder even when USERPROFILE/user.home differ.
            // This changes only this child JVM; no registry or OS folder mutation.
            Field singleton = FileSystemView.class.getDeclaredField("windowsFileSystemView");
            singleton.setAccessible(true);
            singleton.set(null, new TaskFileSystemView(state.toFile(), documents.toFile()));
            if (!FileSystemView.getFileSystemView().getDefaultDirectory().toPath()
                    .toRealPath().equals(documents)) {
                throw new IllegalStateException("Could not install Audiveris task directory policy");
            }
        }
    }

    public static void main(String[] args) throws Throwable {
        configureTaskDirectories();
        // Fixed target, not a general arbitrary-class launcher. Initialize the
        // application only after directory policy installation has succeeded.
        try {
            Class.forName("Audiveris").getMethod("main", String[].class)
                    .invoke(null, (Object) args);
        } catch (InvocationTargetException error) {
            throw error.getCause();
        }
    }

    private static final class TaskFileSystemView extends FileSystemView {
        private final File home;
        private final File documents;

        TaskFileSystemView(File home, File documents) {
            this.home = home;
            this.documents = documents;
        }

        @Override public File getHomeDirectory() { return home; }
        @Override public File getDefaultDirectory() { return documents; }

        @Override public File createNewFolder(File parent) throws IOException {
            throw new IOException("File chooser operations are unavailable in headless OCR");
        }
    }
}
