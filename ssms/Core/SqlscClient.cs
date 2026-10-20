using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace CoopSqlSourceControl.Core
{
    /// <summary>The sqlsc commands the SSMS window uses, one method each, for one repo.</summary>
    public sealed class SqlscClient
    {
        private readonly SqlscRunner runner;
        private readonly string configPath;

        public SqlscClient(SqlscRunner runner, string configPath)
        {
            this.runner = runner;
            this.configPath = configPath;
        }

        public Task<CliResult> TargetsAsync(CancellationToken token)
        {
            return Run(token, "targets");
        }

        public Task<CliResult> StatusAsync(string target, CancellationToken token)
        {
            return Run(token, "status", "--target", target);
        }

        public Task<CliResult> DiffAsync(string target, IEnumerable<string> objects, CancellationToken token)
        {
            return Run(token, new[] { "diff", "--target", target }.Concat(objects));
        }

        public Task<CliResult> CommitAsync(string target, IEnumerable<string> objects, string message, CancellationToken token, bool push = false)
        {
            return Run(token, new[] { "commit", "--target", target }.Concat(Push(push)).Concat(Message(message)).Concat(objects));
        }

        public Task<CliResult> GetLatestAsync(string target, IEnumerable<string> objects, bool dryRun, CancellationToken token)
        {
            var args = new List<string> { "get-latest", "--target", target };
            if (dryRun)
            {
                args.Add("--dry-run");
            }

            return Run(token, args.Concat(objects));
        }

        public Task<CliResult> CheckoutAsync(string target, IEnumerable<string> objects, string note, CancellationToken token)
        {
            return Run(token, new[] { "checkout", "--target", target }.Concat(Message(note)).Concat(objects));
        }

        public Task<CliResult> CheckinAsync(string target, IEnumerable<string> objects, string message, bool commit, CancellationToken token, bool push = false)
        {
            var args = new List<string> { "checkin", "--target", target };
            if (!commit)
            {
                args.Add("--no-commit");
            }
            else if (push)
            {
                args.Add("--push");
            }

            return Run(token, args.Concat(Message(message)).Concat(objects));
        }

        public Task<CliResult> DataAddAsync(string target, IEnumerable<string> tables, CancellationToken token)
        {
            return Run(token, new[] { "data", "add", "--target", target }.Concat(tables));
        }

        public Task<CliResult> LocksAsync(string target, CancellationToken token)
        {
            return Run(token, "locks", "--target", target);
        }

        public Task<CliResult> UnlockAsync(string target, IEnumerable<string> objects, bool force, CancellationToken token)
        {
            var args = new List<string> { "unlock", "--target", target };
            if (force)
            {
                args.Add("--force");
            }

            return Run(token, args.Concat(objects));
        }

        private static IEnumerable<string> Push(bool push)
        {
            return push ? new[] { "--push" } : Enumerable.Empty<string>();
        }

        private static IEnumerable<string> Message(string message)
        {
            return string.IsNullOrWhiteSpace(message) ? Enumerable.Empty<string>() : new[] { "-m", message.Trim() };
        }

        private Task<CliResult> Run(CancellationToken token, params string[] args)
        {
            return runner.RunAsync(configPath, args, token);
        }

        private Task<CliResult> Run(CancellationToken token, IEnumerable<string> args)
        {
            return runner.RunAsync(configPath, args.ToList(), token);
        }
    }
}
