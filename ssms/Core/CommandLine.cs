using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace CoopSqlSourceControl.Core
{
    /// <summary>
    /// Builds a Windows command line from separate arguments, quoted the way
    /// CommandLineToArgvW (and Python's own argument parsing) splits them again.
    /// .NET Framework's ProcessStartInfo only takes one string, so object names
    /// with spaces or quotes have to survive this round trip.
    /// </summary>
    public static class CommandLine
    {
        public static string Join(IEnumerable<string> arguments)
        {
            return string.Join(" ", arguments.Select(Quote));
        }

        public static string Quote(string argument)
        {
            if (argument.Length > 0 && argument.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            {
                return argument;
            }

            var quoted = new StringBuilder("\"");
            var backslashes = 0;
            foreach (var ch in argument)
            {
                if (ch == '\\')
                {
                    backslashes++;
                    continue;
                }

                if (ch == '"')
                {
                    // Backslashes before a quote are doubled, and the quote itself escaped.
                    quoted.Append('\\', backslashes * 2 + 1);
                }
                else
                {
                    quoted.Append('\\', backslashes);
                }

                backslashes = 0;
                quoted.Append(ch);
            }

            // Backslashes before the closing quote are doubled so they stay literal.
            quoted.Append('\\', backslashes * 2);
            quoted.Append('"');
            return quoted.ToString();
        }
    }
}
