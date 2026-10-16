# Azure data services version & end-of-support dashboard

Know which Azure data services run old versions or services that are being retired, and get an email before support ends. It works in any Azure tenant, deploys from one template, and nothing in the code needs editing.

<!-- To pin a release, replace main with the release tag (for example v5.1.0) in the links below. -->
[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fjputhooran%2Fazure-data-eol-tracker%2Fmain%2Fazuredeploy.json)

- [Why](#why)
- [How it works](#how-it-works)
- [What is tracked](#what-is-tracked)
- [What gets deployed](#what-gets-deployed)
- [Deploy](#deploy)
- [After deployment](#after-deployment)
- [Using the dashboard](#using-the-dashboard)
- [Reminder emails](#reminder-emails)
- [Settings](#settings)
- [Updating](#updating)
- [Cost](#cost)
- [Security and permissions](#security-and-permissions)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Removing it](#removing-it)
- [Repository layout](#repository-layout)
- [Support](#support)
- [License](#license)

## Why

Database versions reach the end of support on a fixed schedule. When a version passes that date, Azure moves it into paid Extended Support (PostgreSQL, MySQL) or stops security updates unless you buy Extended Security Updates (SQL Server). Some whole services are retired, such as Azure Cache for Redis and the single servers. The dates are published, but they're spread across many pages and easy to miss. This asset brings them together, matches them against what you actually run, and reminds the right people in time to plan an upgrade.

## How it works

1. **Every week** a small Logic App starts the update job, a Python runbook in Azure Automation.
2. The job reads Microsoft's published support dates:
   - the Microsoft Learn version policy pages for PostgreSQL and MySQL
   - the Microsoft Lifecycle page of each SQL Server version
   - the Cosmos DB for PostgreSQL supported versions page
   - a list of service retirement dates kept in the runbook
3. It saves the dates in the dashboard and stamps the date it checked them.
4. It lists your data services with Azure Resource Graph and finds everything that is past end of support or will reach it within the Red threshold (180 days by default).
5. If anything needs attention, it emails one summary to the database team and a personal list to each owner, through a Logic App and an Office 365 Outlook connection. If nothing needs attention, nothing is sent.
6. The dashboard shows every data service with its version, support dates and status. It's built on Azure Resource Graph, so it's always current and costs nothing to view.

If a source page can't be read or its dates look wrong, the job changes nothing and ends as Failed. The dashboard keeps its last good dates, and its banner turns orange when they're more than 45 days old.

## What is tracked

### Services

| Service | What's shown | Where the dates come from |
|---|---|---|
| Azure Database for PostgreSQL flexible server | Version, end of standard support, end of Extended Support | Microsoft Learn version policy and extended support pages |
| Azure Database for MySQL flexible server | Version, end of standard support, end of Extended Support | Microsoft Learn version policy page |
| SQL Server enabled by Azure Arc, SQL Server on Azure VMs | Version, edition, end of extended support (free security updates stop), ESU end, ESU status (Arc) | Microsoft Lifecycle page of each SQL Server version |
| Azure Cosmos DB for PostgreSQL | PostgreSQL version and its retirement date | Supported versions page |
| Azure Cache for Redis (Basic, Standard, Premium) | Redis version; the service retires 30 Sep 2028 | Retirement FAQ |
| Azure Cache for Redis (Enterprise, Enterprise Flash) | Redis version; the service retires 31 Mar 2027 | Retirement FAQ |
| PostgreSQL single server, MySQL single server, MariaDB | Shown as past end of support (retired services) | Retirement announcements |
| Azure Cosmos DB for MongoDB (RU), Azure DocumentDB | MongoDB version; *Review* below a version you choose | No end-of-support schedule to read |
| Azure Managed Instance for Apache Cassandra | Cassandra version; *Review* below a version you choose | No end-of-support schedule to read |
| Azure Managed Redis | Redis version | No end-of-support schedule to read |
| Azure SQL Managed Instance | Update policy | Update policy documentation |
| Azure SQL Database; Cosmos DB for NoSQL, Cassandra, Table and Gremlin | Listed so the inventory is complete | Nothing to upgrade |

The SQL server that Azure Synapse Analytics creates for each workspace, in a resource group Synapse manages, is left out.

### Status

| Status | Meaning |
|---|---|
| Past end of support | Support has ended, or the service has been retired. PostgreSQL and MySQL run in paid Extended Support. SQL Server only gets security updates through Extended Security Updates (ESU). |
| Red | Support ends within the Red threshold (180 days by default). These are emailed. |
| Amber | Support ends within the Amber threshold (365 days by default). |
| Green | More support time left than the Amber threshold. |
| Review | An older MongoDB or Cassandra version (below the version you choose), or the version isn't reported. There's no end date, so this is for upgrade planning. |
| No end date | Microsoft hasn't published an end date for this version yet. |
| Not version-based | Nothing to upgrade. SQL Managed Instance follows an update policy instead. |

**Support ends** means:
- **PostgreSQL and MySQL:** the end of Azure standard support.
- **SQL Server:** the end of Microsoft extended support, when free security updates stop.
- **Cosmos DB for PostgreSQL:** the version's retirement date.
- **Redis, single servers and MariaDB:** the service's retirement date.

**Paid support ends** is when Azure Extended Support (PostgreSQL, MySQL) or Extended Security Updates (SQL Server) end.

## What gets deployed

| Resource | Default name | Purpose |
|---|---|---|
| Workbook | Azure data services version & end-of-support dashboard | The dashboard |
| Logic App with an Office 365 Outlook connection | `la-db-eol-reminders` | Sends the reminder emails |
| Automation account with a managed identity | `aa-db-eol-dashboard` | Runs the update job |
| Runtime environment (Python 3.10) and runbook | `python-310-db-eol`, `update_db_eol_dashboard_dates` | The update job, imported from the `runbook` folder of this repository |
| Logic App with a managed identity | `la-db-eol-weekly-run` | Starts the update job once a week |

| Identity | Role | Scope | Assigned by |
|---|---|---|---|
| Automation account | Workbook Contributor | The resource group | The template |
| Automation account | Logic App Contributor | The reminder Logic App | The template |
| Automation account | Reader | The subscriptions or management group to check | **You** (see [After deployment](#after-deployment)) |
| Weekly-run Logic App | Automation Operator | The Automation account | The template |

## Deploy

### Before you start

- A resource group for these resources, for example `rg-data-eol`.
- **Owner** or **User Access Administrator** on that resource group, so the template can assign roles.
- Rights to assign **Reader** on the subscriptions, or on a management group above them, that the job should check.
- A mailbox to send from. This is the account that authorizes the Outlook connection, or a shared mailbox it has Send As rights on.
- The address of the database team that should get the weekly summary.

### Option 1: Deploy to Azure button

1. Select **Deploy to Azure** at the top of this page.
2. Choose the subscription, resource group and region.
3. Fill in **Team Emails**, for example `dbteam@contoso.com; dba-lead@contoso.com`.
4. Optionally set:
   - **Sender Mailbox** (a shared mailbox to send from)
   - **Weekly Run Day**, **Weekly Run Hour** and **Weekly Run Time Zone**
5. Select **Review + create**.

### Option 2: Azure CLI

```bash
az group create -n rg-data-eol -l westeurope
az deployment group create -g rg-data-eol \
  --template-uri https://raw.githubusercontent.com/jputhooran/azure-data-eol-tracker/main/azuredeploy.json \
  --parameters teamEmails="dbteam@contoso.com" weeklyRunTimeZone="W. Europe Standard Time"
```

### Option 3: Azure PowerShell

```powershell
New-AzResourceGroup -Name rg-data-eol -Location westeurope
New-AzResourceGroupDeployment -ResourceGroupName rg-data-eol `
  -TemplateUri https://raw.githubusercontent.com/jputhooran/azure-data-eol-tracker/main/azuredeploy.json `
  -teamEmails "dbteam@contoso.com" -weeklyRunTimeZone "W. Europe Standard Time"
```

The template imports the update job from the same place it was loaded from, so the two always match. The **runbook** output shows the address it used.

### Template parameters

| Parameter | Default | Description |
|---|---|---|
| `teamEmails` | (empty) | Who gets the weekly summary, separated by semicolons. You can change it later in the dashboard. |
| `senderMailbox` | (empty) | Optional shared mailbox to send from. The account that authorizes the Outlook connection needs Send As rights on it. |
| `weeklyRunDay` | Monday | Day of the weekly run |
| `weeklyRunHour` | 7 | Hour of the weekly run (0-23) |
| `weeklyRunTimeZone` | UTC | Windows time zone name, for example `India Standard Time`, `GMT Standard Time` or `Pacific Standard Time` |
| `scheduleWeeklyRun` | true | Set to false if you start the job some other way |
| `createAutomationAccount` | true | Set to false to use an existing Automation account in the same resource group |
| `automationAccountName` | aa-db-eol-dashboard | Account to create, or your existing account |
| `automationAccountPrincipalId` | (empty) | Only with an existing account: its managed identity's object ID, so the template can give it the two roles |
| `createRunbook` | true | Import and publish the update job |
| `runbookUrl` | (empty) | Only when deploying from a downloaded copy: the raw address of `runbook/update_db_eol_dashboard_dates.py` |
| `workbookId` | (new GUID) | Set it to an existing dashboard's GUID to update that dashboard in place |
| `workbookDisplayName` | Azure data services version & end-of-support dashboard | Name in the Workbooks gallery |
| `logicAppName`, `weeklyRunLogicAppName` | la-db-eol-reminders, la-db-eol-weekly-run | Names of the two Logic Apps |

### Deploying from a downloaded copy

When you deploy `azuredeploy.json` from a file, the template can't tell where the runbook is. This happens with **Build your own template** in the portal, `--template-file`, or `-TemplateFile`. Use one of these:
- Set `runbookUrl` to the raw address of `runbook/update_db_eol_dashboard_dates.py`.
- Or import the runbook yourself:
  1. In the Automation account, open **Runbooks**, then **Import a runbook**, and upload the file.
  2. Choose type **Python**, runtime **3.10** and the name `update_db_eol_dashboard_dates`.
  3. Select **Publish**.

  The weekly Logic App starts the runbook by that name.

## After deployment

1. **Authorize the Outlook connection.**
   - In the resource group, open the API connection named in the `outlookConnectionToAuthorize` output (`la-db-eol-reminders-office365` by default).
   - Select **Edit API connection**, then **Authorize**, and sign in with the sending account.
   - Select **Save**.
2. **Let the job read your data services.** Give the Automation account's identity (the `automationAccountPrincipalId` output) Reader where your databases are:
   ```bash
   az role assignment create --assignee-object-id <principal-id> --assignee-principal-type ServicePrincipal \
     --role Reader --scope /providers/Microsoft.Management/managementGroups/<management-group-id>
   ```
   You can use `--scope /subscriptions/<subscription-id>` instead, once for each subscription.
3. **Run it once now (optional).** Otherwise it first runs at the weekly time.
   - In the Automation account, open **Runbooks**, then `update_db_eol_dashboard_dates`, then **Start**.
   - To test without saving or sending anything, enter `--dry-run` as the first parameter.
4. **Check it worked.**
   - The job ends as **Completed**, and its output lists the dates it read and who was emailed.
   - The dashboard banner shows today's date as the last check.
   - The `la-db-eol-weekly-run` Logic App is enabled. You can select **Run trigger** to test that it starts a job.

## Using the dashboard

Open **Azure Monitor**, then **Workbooks**, then **Azure data services version & end-of-support dashboard**.

| Tab | What it shows |
|---|---|
| Database versions | Status tiles, a summary by resource group (most at risk first), the **Versions in use** table, and the full server list with support dates, days left, ESU status, owner and what to do |
| Microsoft retirement notices | Retirement recommendations from Azure Advisor and retirement notices from Azure Service Health |
| Lifecycle dates & how to use | Every support date in use, with links to Microsoft's sources |

- **Filters:** subscriptions, database types, resource groups, status, and any tag (for example `Environment = Production`).
- **Versions in use:** select a row to list only the servers on that version, or select **Show all versions** to go back.
- **Owners:** put an email address in each resource's `Owner` tag, or the tag you choose in the settings. For SQL Server, the tag on its Arc machine or VM counts too. Owners then get their own reminder list.

## Reminder emails

- **When:** once a week, and only if something is Red or past end of support.
- **Who:**
  - one summary to **Reminder emails to** (set in the dashboard or the template)
  - each owner's own list to the address in their owner tag
- **What:** a table showing:
  - server, status, engine and version
  - support end, days left and paid support end
  - ESU status (SQL Server)
  - subscription, resource group and owner
  - a short "what to do" note

  The subject reads like *Action needed: 3 database servers are near or past end of support*.
- **To turn off:** clear **Reminder emails to**, or set `SEND_REMINDERS` to False in the runbook.

## Settings

### In the dashboard

Change these at the top of the **Database versions** tab. Select **Edit**, change the setting, then select **Save**. The update job reads them every week, so the emails always match the dashboard.

| Setting | Default | Used for |
|---|---|---|
| Red within (days) | 180 | Status, and which servers are emailed |
| Amber within (days) | 365 | Status |
| Owner tag name | Owner | The Owner column and owner emails |
| MongoDB: review versions below | 5.0 | *Review* status for Cosmos DB for MongoDB and Azure DocumentDB |
| Cassandra: review versions below | 4.0 | *Review* status for Managed Instance for Apache Cassandra |
| Reminder emails to | (from the template) | Who gets the weekly summary. Leave it empty to turn the emails off. |

### Weekly run

To change when the job runs, redeploy with new `weeklyRun*` values, or edit the Recurrence trigger of `la-db-eol-weekly-run`. That Logic App's run history shows each start. The job's output is under the runbook's **Jobs**.

### In the runbook (optional)

These live in the SETTINGS section of `runbook/update_db_eol_dashboard_dates.py`. A redeploy replaces the runbook with the repository version. To keep changes, make them in your own copy of the repository and deploy from there.

| Setting | Use |
|---|---|
| `DATE_OVERRIDES` | Set a date yourself, for example `{("PostgreSQL", "14"): {"standardSupportEnds": "2027-01-31"}}` or `{("Azure Cache for Redis", "all"): {"standardSupportEnds": "2028-09-30"}}` |
| `SERVICE_RETIREMENTS` | Retirement dates of whole services |
| `SOURCE` | Use `endoflife.date` instead of Microsoft Learn |
| `INCLUDE_PAST_SUPPORT`, `EMAIL_OWNERS`, `SEND_REMINDERS` | What the emails include and who gets them |
| `WORKBOOK_RESOURCE_ID` | Only needed if there are several copies of the dashboard |

## Updating

1. Redeploy the template (button, CLI or PowerShell) into the same resource group. Set `workbookId` to the GUID of your existing dashboard (it's in the dashboard's resource ID) so it's updated in place rather than copied.
2. The runbook is imported again from the repository.
3. Dashboard settings return to their defaults, so set them again.

If you set the solution up before the template imported the runbook:
- If you created the Automation account yourself, set `createAutomationAccount` to false and put its name in `automationAccountName`. It must be in the same resource group.
- If you linked an Automation schedule to the runbook, unlink it (runbook › **Schedules**), or set `scheduleWeeklyRun` to false. Otherwise the job runs twice a week.
- Earlier values of `SUBSCRIPTION_ID`, `RESOURCE_GROUP` and `TEAM_EMAILS` in the runbook aren't needed any more.

## Cost

| Component | Cost |
|---|---|
| Dashboard and Azure Resource Graph | No charge |
| Automation | The weekly job runs for a few minutes a month, well within the 500 free job minutes per subscription |
| Logic Apps (Consumption) | Charged per action: two actions a week to start the job, plus one per email sent. A few cents a month at most. |

## Security and permissions

- No passwords, keys or secrets are stored.
  - The update job signs in with the Automation account's managed identity.
  - The weekly Logic App signs in with its own managed identity.
  - The reminder Logic App's trigger address is fetched when needed, never saved.
- Each identity gets only what it needs:
  - Reader to list data services.
  - Workbook Contributor to save the dates.
  - Logic App Contributor on one Logic App, to send through it.
  - Automation Operator on one Automation account, to start the job.
- The job reads only public Microsoft pages and Azure resource metadata. It never connects to your databases or reads their data.
- Emails go out only through the Outlook connection that you authorize.

## Limitations

- The dates are only as good as Microsoft's published pages. If a page changes layout, the job stops without changing anything.
- There's no end-of-support schedule to read for Cosmos DB for MongoDB, Azure DocumentDB, Managed Instance for Apache Cassandra or Azure Managed Redis versions. These show *Review* or *No end date* and aren't emailed.
- Paid support (Extended Support) dates for newer PostgreSQL versions appear once Microsoft publishes them.
- The dashboard can't see SQL Server ESUs bought through volume licensing, or ESU subscriptions on Azure VMs. (SQL Server 2014 on Azure VMs gets ESUs free.) Those servers show *Not reported* or *Not subscribed (Azure Arc)*.
- Viewers only see resources they can read. The emails cover what the Automation account's identity can read.
- Automation runtime environments aren't available in Brazil Southeast or Azure Government. There, set `createRunbook` to false and import the runbook yourself.
- Analytics services (HDInsight, Synapse Spark, Azure Databricks) aren't tracked.
- Tag values that contain quotes or backslashes aren't offered in the tag filter.

## Troubleshooting

| What you see | What to do |
|---|---|
| The deployment fails on the runbook with a download error | Azure couldn't read the runbook address. The repository must be public, or set `runbookUrl` to an address Azure can read. |
| A weekly run in `la-db-eol-weekly-run` fails with AuthorizationFailed | The Automation Operator role can take a few minutes to apply after deployment. Check that it's on the Automation account. |
| Job output: Couldn't find the dashboard | Check that the identity has Workbook Contributor on the dashboard's resource group. It can take a few minutes to apply. |
| Job output: Found 2 copies of the dashboard | Delete the copy you don't use, or set `WORKBOOK_RESOURCE_ID`. |
| Job output: can't see any subscriptions / isn't allowed to query Azure Resource Graph | Assign Reader (see [After deployment](#after-deployment)). |
| Job output: The Logic App couldn't send the email ... Outlook connection | Authorize the connection. If you use a shared mailbox, check the Send As rights. |
| Job output: Reminder emails are off | Fill in **Reminder emails to** in the dashboard and save it. |
| The dashboard banner is orange | The dates haven't been checked for 45 days. Look at the runbook's recent jobs. |
| The job runs twice a week | An older Automation schedule is still linked to the runbook. Unlink it. |
| RoleAssignmentExists when redeploying | Set `createAutomationAccount` to false and leave `automationAccountPrincipalId` empty. |

## Removing it

1. Remove the Reader role you assigned:
   ```bash
   az role assignment delete --assignee <principal-id> --role Reader --scope <scope>
   ```
2. Delete the workbook, both Logic Apps, the Office 365 connection and the Automation account. Or delete the resource group if it holds nothing else.
3. Any role assignments left behind show as "Identity not found" and can be deleted.

## Repository layout

| Path | What it is |
|---|---|
| `azuredeploy.json` | Deployment template: dashboard, Logic Apps, Outlook connection, Automation account, runbook and roles |
| `runbook/update_db_eol_dashboard_dates.py` | The update job (Python 3.10, standard library only) |
| `LICENSE` | MIT License |
| `workbook/azure-db-eol-dashboard-workbook.json` | The dashboard on its own, to paste into a workbook's Advanced Editor if you don't use the template |

**Releases:** tag each release (for example `v5.1.0`). To pin customers to a version, use the tag instead of `main` in the Deploy to Azure link and the commands above. The template always imports the runbook from the same tag.

## Support

This project isn't an official Microsoft product. It isn't covered by Microsoft Support or by any Microsoft service-level agreement. For questions, bugs or ideas, open an issue in this repository. The Azure services it deploys (Azure Monitor workbooks, Azure Automation, Logic Apps) are supported as usual through your Azure support plan.

## License

Copyright (c) Microsoft Corporation. Released under the [MIT License](LICENSE). The software is provided "as is", without warranty of any kind.
