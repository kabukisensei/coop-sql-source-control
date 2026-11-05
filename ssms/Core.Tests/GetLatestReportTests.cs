using CoopSqlSourceControl.Core;
using Newtonsoft.Json.Linq;
using Xunit;

namespace CoopSqlSourceControl.Core.Tests
{
    public class GetLatestReportTests
    {
        private static JObject Answer(string json)
        {
            return JObject.Parse(json);
        }

        [Fact]
        public void SaysUpdatedWhenNothingFailed()
        {
            var text = GetLatestReport.Describe("dev", Answer(@"{""ok"": true, ""failed"": [], ""done"": [""[dbo].[vA]""], ""transaction"": true,
                ""kept"": [{""object"": ""[dbo].[vC]""}]}"));
            Assert.StartsWith("Updated dev.", text);
            Assert.Contains("[dbo].[vC]", text);
        }

        [Fact]
        public void AFullRollbackSaysNothingChanged()
        {
            var text = GetLatestReport.Describe("dev", Answer(@"{""ok"": false, ""transaction"": true, ""done"": [],
                ""steps"": [{""action"": ""create"", ""object"": ""[dbo].[vA]""}, {""action"": ""create"", ""object"": ""[dbo].[vB]""}],
                ""failed"": [{""object"": ""[dbo].[vB]"", ""error"": ""Type BadType is not a defined system type.""}]}"));
            Assert.Contains("Get latest failed on dev: [dbo].[vB]: Type BadType is not a defined system type.", text);
            Assert.Contains("Every change was rolled back, so dev is as it was.", text);
            Assert.DoesNotContain("stay applied", text);
        }

        [Fact]
        public void APartialRunWithoutTransactionsListsWhatStays()
        {
            var text = GetLatestReport.Describe("wh", Answer(@"{""ok"": false, ""transaction"": false, ""done"": [""[dbo].[vA]""],
                ""steps"": [{""action"": ""create"", ""object"": ""[dbo].[vA]""}, {""action"": ""create"", ""object"": ""[dbo].[vB]""},
                            {""action"": ""create"", ""object"": ""[dbo].[vC]""}],
                ""held_back"": [{""action"": ""manual"", ""object"": ""[dbo].[T]""}],
                ""failed"": [{""object"": ""[dbo].[vB]"", ""error"": ""boom""}]}"));
            Assert.DoesNotContain("nothing was changed", text);
            Assert.Contains("wh has no transactions, so nothing was rolled back.", text);
            Assert.Contains("These stay applied: [dbo].[vA], and the failed step may have run part way.", text);
            Assert.Contains("Not applied: [dbo].[vC].", text);
            Assert.Contains("Held back for a manual change: [dbo].[T].", text);
            Assert.Contains("Refresh and review the differences before you retry.", text);
        }

        [Fact]
        public void AFirstStepFailingWithoutTransactionsMayHaveRunPartWay()
        {
            var text = GetLatestReport.Describe("wh", Answer(@"{""ok"": false, ""transaction"": false, ""done"": [],
                ""failed"": [{""object"": ""[dbo].[vB]"", ""error"": ""boom""}]}"));
            Assert.Contains("No step had finished, and the failed step may have run part way.", text);
            Assert.DoesNotContain("as it was", text);
        }

        [Fact]
        public void ABeforeMigrationCommittedAheadOfAFailureStaysApplied()
        {
            var text = GetLatestReport.Describe("dev", Answer(@"{""ok"": false, ""transaction"": true,
                ""done"": [""Migrations/202601010000_fill.sql""],
                ""steps"": [{""action"": ""migrate"", ""object"": ""Migrations/202601010000_fill.sql""},
                            {""action"": ""create"", ""object"": ""[dbo].[vA]""}, {""action"": ""create"", ""object"": ""[dbo].[vB]""}],
                ""failed"": [{""object"": ""[dbo].[vB]"", ""error"": ""boom""}]}"));
            Assert.Contains("The rest was rolled back, but these were committed before it and stay applied: Migrations/202601010000_fill.sql.", text);
            Assert.Contains("Not applied: [dbo].[vA].", text);
            Assert.DoesNotContain("as it was", text);
        }

        [Fact]
        public void AnAnswerWithoutTheOutcomeSaysItIsUnknown()
        {
            var text = GetLatestReport.Describe("dev", Answer(@"{""ok"": false, ""failed"": [{""object"": ""[dbo].[vB]"", ""error"": ""boom""}]}"));
            Assert.Contains("is not known", text);
            Assert.DoesNotContain("rolled back", text);
        }

        [Fact]
        public void NoAnswerAtAllIsAnUnknownOutcome()
        {
            var text = GetLatestReport.Unknown("dev", "Cancelled.");
            Assert.StartsWith("Cancelled. ", text);
            Assert.Contains("Whether get latest changed dev before it stopped is not known.", text);
            Assert.DoesNotContain("nothing was changed", GetLatestReport.Unknown("dev", null));
        }
    }
}
