using System;
internal static class NotificationTests
{
    private static void Check(bool value) { if (!value) throw new Exception("Notification regression"); }
    public static void Main()
    {
        var start = DateTimeOffset.Parse("2026-09-13T00:00:00Z");
        var tracker = new CompletionTracker(start);
        var task = new CompletionTask { Id = "test-id", Status = "completed", UpdatedAt = start.AddSeconds(-1).ToString("o") };
        Check(!tracker.Take(task));
        task.UpdatedAt = start.AddSeconds(1).ToString("o");
        Check(tracker.Take(task));
        Check(!tracker.Take(task));
        task.UpdatedAt = start.AddSeconds(2).ToString("o");
        Check(!tracker.Take(task)); // A title edit must not repeat completion.
        task.Status = "running";
        Check(!tracker.Take(task));
        task.Status = "completed";
        Check(tracker.Take(task)); // A continued task can finish again.
        task.Id = "failed-task";
        task.Status = "failed";
        Check(!tracker.Take(task));
        task.Status = "cancelled";
        Check(!tracker.Take(task));
        task.Status = "completed";
        task.UpdatedAt = "invalid";
        Check(!tracker.Take(task));
        Check(!tracker.Take(null));
        task.UpdatedAt = start.AddSeconds(3).ToString("o");
        task.Id = "../bad?task=x";
        Check(!tracker.Take(task));
        Console.WriteLine("NOTIFICATION_TESTS_OK");
    }
}
