import base64
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from html import unescape
from tkinter import (
    Tk,
    Toplevel,
    StringVar,
    BooleanVar,
    Text,
    END,
    BOTH,
    LEFT,
    RIGHT,
    X,
    W,
    EW,
    N,
    messagebox,
    filedialog,
)
from tkinter import ttk
from xml.etree import ElementTree as ET

import requests


# ============================================================
# CONFIG
# ============================================================

API_VERSION = "7.1"

WAIT_TIMEOUT = 180
POLL_SECONDS = 3

DEFAULT_ORG = "lloydsregistergroup"

EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
    ".xls": "application/vnd.ms-excel",
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet"
    ),
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".zip": "application/zip",
}


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def clean_html(value):
    value = unescape(str(value or ""))

    value = re.sub(
        r"<br\s*/?>",
        "\n",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"</p\s*>",
        "\n",
        value,
        flags=re.I,
    )

    return re.sub(
        r"<[^>]+>",
        "",
        value,
    ).strip()


def ado_outcome(status):
    """
    Convert UI status to Azure DevOps TestOutcome.
    """

    mapping = {
        "Passed": "Passed",
        "Failed": "Failed",
        "Skipped": "NotExecuted",
    }

    return mapping.get(
        status,
        "NotExecuted",
    )


def result_point_id(result):
    point = result.get(
        "testPoint"
    ) or {}

    value = (
        point.get("id")
        or result.get("testPointId")
        or ""
    )

    return str(value)


def result_map(results):
    mapping = {}

    for result in results:
        if not result.get("id"):
            continue

        point_id = result_point_id(result)

        if not point_id:
            continue

        mapping[point_id] = result

    return mapping


# ============================================================
# AZURE DEVOPS CLIENT
# ============================================================

class ADOClient:

    def __init__(
        self,
        organization,
        project,
        pat,
        debug=False,
    ):
        self.organization = organization.strip()
        self.project = project.strip()
        self.debug = debug

        self.base = (
            f"https://dev.azure.com/"
            f"{self.organization}/"
            f"{self.project}"
        )

        self.session = requests.Session()

        self.session.auth = (
            "",
            pat,
        )

        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # ========================================================
    # GENERIC REQUEST
    # ========================================================

    def request(
        self,
        method,
        path,
        params=None,
        body=None,
        timeout=120,
    ):
        params = dict(
            params or {}
        )

        params["api-version"] = API_VERSION

        url = self.base + path

        if self.debug:
            print()
            print("=" * 80)
            print(method, url)
            print("PARAMS:", params)

            if body is not None:
                print("BODY:")
                print(body)

        response = self.session.request(
            method,
            url,
            params=params,
            json=body,
            timeout=timeout,
        )

        if self.debug:
            print(
                "HTTP:",
                response.status_code,
            )

            print(
                "RESPONSE:",
                response.text[:10000],
            )

        if not response.ok:
            raise RuntimeError(
                f"Azure DevOps API error "
                f"{response.status_code}: "
                f"{response.text[:5000]}"
            )

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError:
            return response.text

    # ========================================================
    # TEST PLAN
    # ========================================================

    def get_suite(
        self,
        plan,
        suite,
    ):
        return self.request(
            "GET",
            f"/_apis/testplan/Plans/{plan}/suites/{suite}",
        )

    def get_points(
        self,
        plan,
        suite,
    ):
        all_points = []

        skip = 0
        top = 1000

        while True:

            data = self.request(
                "GET",
                f"/_apis/test/Plans/{plan}/Suites/{suite}/points",
                params={
                    "$skip": skip,
                    "$top": top,
                    "includePointDetails": "true",
                },
            )

            points = data.get(
                "value",
                [],
            )

            all_points.extend(
                points
            )

            if len(points) < top:
                break

            skip += len(points)

        return all_points

    # ========================================================
    # WORK ITEMS
    # ========================================================

    def get_work_item(
        self,
        test_case_id,
    ):
        fields = ",".join([
            "System.Id",
            "System.Title",
            "System.Description",
            "Microsoft.VSTS.TCM.Steps",
        ])

        return self.request(
            "GET",
            f"/_apis/wit/workitems/{test_case_id}",
            params={
                "fields": fields,
            },
        )

    def get_steps(
        self,
        test_case_id,
    ):
        fields = self.get_work_item(
            test_case_id
        ).get(
            "fields",
            {},
        )

        xml_text = fields.get(
            "Microsoft.VSTS.TCM.Steps",
            "",
        )

        if not xml_text:
            return []

        try:
            root = ET.fromstring(
                xml_text
            )
        except ET.ParseError:
            return []

        steps = []

        for number, step in enumerate(
            root.findall("./step"),
            1,
        ):
            step_id = safe_int(
                step.get("id"),
                number,
            )

            strings = step.findall(
                "./parameterizedString"
            )

            action = ""

            expected = ""

            if strings:
                action = clean_html(
                    strings[0].text
                )

            if len(strings) > 1:
                expected = clean_html(
                    strings[1].text
                )

            steps.append({
                "number": number,
                "identifier": str(number),
                "action_path": (
                    f"{step_id:08x}"
                ),
                "action": action,
                "expected": expected,
            })

        return steps

    # ========================================================
    # BUILD
    # ========================================================

    def find_build(
        self,
        build_number,
    ):
        data = self.request(
            "GET",
            "/_apis/build/builds",
            params={
                "buildNumber": build_number,
                "$top": 50,
                "queryOrder": (
                    "finishTimeDescending"
                ),
            },
        )

        for build in data.get(
            "value",
            [],
        ):
            if (
                build.get("buildNumber")
                == build_number
            ):
                return build

        raise RuntimeError(
            f'Build "{build_number}" was not found.'
        )

    # ========================================================
    # TEST RUN
    # ========================================================

    def create_run(
        self,
        plan_id,
        point_ids,
        build,
    ):
        build_id = int(
            build["id"]
        )

        build_number = build.get(
            "buildNumber",
            "",
        )

        unique_points = list(
            dict.fromkeys(
                int(x)
                for x in point_ids
            )
        )

        body = {
            "name": (
                "QA Execution - "
                f"{build_number} - "
                f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            ),

            "plan": {
                "id": str(plan_id),
            },

            "pointIds": unique_points,

            "automated": False,

            "state": "InProgress",

            "build": {
                "id": str(build_id),
                "name": build_number,
            },

            "comment": (
                "Created by QA Test "
                "Execution Manager."
            ),
        }

        return self.request(
            "POST",
            "/_apis/test/runs",
            body=body,
        )

    # ========================================================
    # GET RESULTS
    # ========================================================

    def get_results(
        self,
        run_id,
    ):
        """
        Get ALL results for this run.

        We intentionally do not filter by outcome.
        """

        all_results = []

        skip = 0
        top = 200

        while True:

            data = self.request(
                "GET",
                f"/_apis/test/Runs/{run_id}/results",
                params={
                    "$skip": skip,
                    "$top": top,
                },
            )

            results = data.get(
                "value",
                [],
            )

            all_results.extend(
                results
            )

            if len(results) < top:
                break

            skip += len(results)

        return all_results

    # ========================================================
    # GET SINGLE RESULT
    # ========================================================

    def get_result(
        self,
        run_id,
        result_id,
    ):
        return self.request(
            "GET",
            f"/_apis/test/Runs/"
            f"{run_id}/results/"
            f"{result_id}",
        )

    # ========================================================
    # WAIT FOR RESULTS
    # ========================================================

    def wait_results(
        self,
        run_id,
        point_ids,
    ):
        expected = {
            str(x)
            for x in point_ids
        }

        started = time.time()

        while True:

            results = self.get_results(
                run_id
            )

            mapping = result_map(
                results
            )

            found = (
                expected
                & set(mapping.keys())
            )

            if expected <= set(
                mapping.keys()
            ):
                return results

            if (
                time.time() - started
                > WAIT_TIMEOUT
            ):
                missing = (
                    expected
                    - set(mapping.keys())
                )

                raise TimeoutError(
                    "Timed out waiting for "
                    "Azure DevOps results. "
                    f"Missing: "
                    f"{sorted(missing)}"
                )

            time.sleep(
                POLL_SECONDS
            )

    # ========================================================
    # UPDATE RESULTS
    # ========================================================

    def update_results(
        self,
        run_id,
        updates,
    ):
        if not updates:
            return None

        return self.request(
            "PATCH",
            f"/_apis/test/Runs/{run_id}/results",
            body=updates,
        )

    # ========================================================
    # VERIFY RESULT OUTCOME
    # ========================================================

    def verify_result_outcome(
        self,
        run_id,
        result_id,
        expected_outcome,
    ):
        """
        Re-read the result from Azure DevOps
        after PATCH.

        This is important because the UI should
        only report success if Azure DevOps has
        actually stored the selected outcome.
        """

        result = self.get_result(
            run_id,
            result_id,
        )

        actual = result.get(
            "outcome"
        )

        return (
            actual == expected_outcome,
            actual,
            result,
        )

    # ========================================================
    # COMPLETE RUN
    # ========================================================

    def complete_run(
        self,
        run_id,
    ):
        return self.request(
            "PATCH",
            f"/_apis/test/runs/{run_id}",
            body={
                "state": "Completed",
            },
        )

    # ========================================================
    # ATTACHMENTS
    # ========================================================

    def upload_attachment(
        self,
        run_id,
        result_id,
        path,
    ):
        with open(
            path,
            "rb",
        ) as f:

            encoded = (
                base64.b64encode(
                    f.read()
                ).decode("ascii")
            )

        body = {
            "stream": encoded,

            "fileName": os.path.basename(
                path
            ),

            "comment": (
                "Uploaded by QA Test "
                "Execution Manager."
            ),

            "attachmentType": (
                "GeneralAttachment"
            ),
        }

        attachment_path = (
            f"/_apis/test/Runs/"
            f"{run_id}/Results/"
            f"{result_id}/attachments"
        )

        return self.request(
            "POST",
            attachment_path,
            body=body,
        )


# ============================================================
# TEST CASE
# ============================================================

class TestCase:

    def __init__(
        self,
        point,
    ):
        case = (
            point.get(
                "testCase"
            )
            or {}
        )

        self.point_id = safe_int(
            point.get("id")
        )

        self.id = str(
            case.get(
                "id",
                "",
            )
        )

        self.title = (
            point.get(
                "testCaseTitle"
            )
            or case.get(
                "name"
            )
            or f"Test Case {self.id}"
        )

        self.configuration = (
            (
                point.get(
                    "configuration"
                )
                or {}
            ).get(
                "name",
                "",
            )
        )

        self.selected = True

        self.steps = []

        # UI outcome.
        self.status = "Passed"

        self.comment = ""

        self.attachments = []


# ============================================================
# ATTACHMENTS
# ============================================================

def attachment_info(
    path,
):
    ext = os.path.splitext(
        path
    )[1].lower()

    return {
        "file_name": os.path.basename(
            path
        ),

        "file_path": path,

        "extension": ext,

        "mime_type": EXTENSIONS.get(
            ext,
            "application/octet-stream",
        ),

        "size": os.path.getsize(
            path
        ),
    }


def build_attachment_map(
    test_cases,
    folder,
):
    mapping = {
        tc.id: []
        for tc in test_cases
    }

    if (
        not folder
        or not os.path.isdir(folder)
    ):
        return (
            mapping,
            [],
            [],
        )

    known = set(
        mapping
    )

    unknown = []

    unsupported = []

    for filename in os.listdir(
        folder
    ):
        path = os.path.join(
            folder,
            filename,
        )

        if not os.path.isfile(
            path
        ):
            continue

        stem, ext = os.path.splitext(
            filename
        )

        ext = ext.lower()

        if ext not in EXTENSIONS:
            unsupported.append(
                filename
            )
            continue

        stem = stem.strip()

        if stem in known:
            mapping[
                stem
            ].append(
                attachment_info(
                    path
                )
            )
        else:
            unknown.append(
                filename
            )

    return (
        mapping,
        unknown,
        unsupported,
    )


# ============================================================
# GUI
# ============================================================

class App:

    def __init__(
        self,
        root,
    ):
        self.root = root

        self.root.title(
            "Azure DevOps Test Execution Manager"
        )

        self.root.geometry(
            "1500x900"
        )

        self.client = None

        self.test_cases = []

        self.by_id = {}

        self.current = None

        self.run_id = None

        self.result_by_point = {}

        self.org = StringVar(
            value=DEFAULT_ORG
        )

        self.project = StringVar()

        self.pat = StringVar()

        self.plan = StringVar()

        self.suite = StringVar()

        self.build = StringVar()

        self.folder = StringVar()

        self.search = StringVar()

        self.filter = StringVar(
            value="All"
        )

        self.status = StringVar(
            value="Ready"
        )

        self.case_status = StringVar(
            value="Passed"
        )

        self.comment = StringVar()

        self.debug = BooleanVar()

        self.create_ui()

    # ========================================================
    # UI
    # ========================================================

    def create_ui(self):

        header = ttk.Frame(
            self.root,
            padding=10,
        )

        header.pack(
            fill=X
        )

        ttk.Label(
            header,
            text=(
                "Azure DevOps "
                "Test Execution Manager"
            ),
            font=(
                "Segoe UI",
                18,
                "bold",
            ),
        ).pack(
            side=LEFT
        )

        ttk.Label(
            header,
            textvariable=self.status,
        ).pack(
            side=RIGHT
        )

        self.create_connection()

        main = ttk.PanedWindow(
            self.root,
            orient="horizontal",
        )

        main.pack(
            fill=BOTH,
            expand=True,
            padx=10,
            pady=5,
        )

        left = ttk.Frame(
            main,
            padding=5,
        )

        middle = ttk.Frame(
            main,
            padding=5,
        )

        right = ttk.Frame(
            main,
            padding=5,
        )

        main.add(
            left,
            weight=3,
        )

        main.add(
            middle,
            weight=4,
        )

        main.add(
            right,
            weight=3,
        )

        self.create_case_list(
            left
        )

        self.create_details(
            middle
        )

        self.create_execution(
            right
        )

    # ========================================================
    # CONNECTION
    # ========================================================

    def create_connection(self):

        frame = ttk.LabelFrame(
            self.root,
            text="Azure DevOps Connection",
            padding=10,
        )

        frame.pack(
            fill=X,
            padx=10,
            pady=5,
        )

        fields = [
            (
                "Organization",
                self.org,
            ),
            (
                "Project",
                self.project,
            ),
            (
                "PAT",
                self.pat,
            ),
            (
                "Plan ID",
                self.plan,
            ),
            (
                "Suite ID",
                self.suite,
            ),
            (
                "Build Number",
                self.build,
            ),
        ]

        for i, (
            label,
            var,
        ) in enumerate(fields):

            row = i // 3

            col = (
                i % 3
            ) * 2

            ttk.Label(
                frame,
                text=label,
            ).grid(
                row=row,
                column=col,
                sticky=W,
                padx=5,
                pady=3,
            )

            ttk.Entry(
                frame,
                textvariable=var,
                show=(
                    "*"
                    if label == "PAT"
                    else ""
                ),
            ).grid(
                row=row,
                column=col + 1,
                sticky=EW,
                padx=5,
                pady=3,
            )

        for col in (
            1,
            3,
            5,
        ):
            frame.columnconfigure(
                col,
                weight=1,
            )

        ttk.Label(
            frame,
            text="Attachment Folder",
        ).grid(
            row=2,
            column=0,
            sticky=W,
            padx=5,
        )

        ttk.Entry(
            frame,
            textvariable=self.folder,
        ).grid(
            row=2,
            column=1,
            columnspan=3,
            sticky=EW,
            padx=5,
        )

        ttk.Button(
            frame,
            text="Browse",
            command=self.browse,
        ).grid(
            row=2,
            column=4,
        )

        ttk.Checkbutton(
            frame,
            text="Debug",
            variable=self.debug,
        ).grid(
            row=2,
            column=5,
        )

        ttk.Button(
            frame,
            text="FETCH TEST CASES",
            command=self.fetch,
        ).grid(
            row=3,
            column=0,
            columnspan=6,
            pady=8,
        )

    # ========================================================
    # TEST CASE LIST
    # ========================================================

    def create_case_list(
        self,
        parent,
    ):

        frame = ttk.LabelFrame(
            parent,
            text="Test Cases",
            padding=5,
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        bar = ttk.Frame(
            frame
        )

        bar.pack(
            fill=X,
            pady=3,
        )

        ttk.Entry(
            bar,
            textvariable=self.search,
        ).pack(
            side=LEFT,
            fill=X,
            expand=True,
        )

        ttk.Combobox(
            bar,
            textvariable=self.filter,
            values=[
                "All",
                "Run",
                "Don't Run",
                "Passed",
                "Failed",
                "Skipped",
                "Has Attachments",
            ],
            state="readonly",
            width=16,
        ).pack(
            side=LEFT,
            padx=5,
        )

        ttk.Button(
            bar,
            text="Filter",
            command=self.refresh_list,
        ).pack(
            side=LEFT
        )

        bulk = ttk.Frame(
            frame
        )

        bulk.pack(
            fill=X,
            pady=3,
        )

        ttk.Button(
            bulk,
            text="RUN ALL",
            command=lambda: self.set_selected(
                True
            ),
        ).pack(
            side=LEFT,
            padx=2,
        )

        ttk.Button(
            bulk,
            text="DON'T RUN ALL",
            command=lambda: self.set_selected(
                False
            ),
        ).pack(
            side=LEFT,
            padx=2,
        )

        columns = (
            "run",
            "id",
            "title",
            "attachments",
            "result",
        )

        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
        )

        headings = {
            "run": "RUN?",
            "id": "Test Case",
            "title": "Title",
            "attachments": "Attach",
            "result": "RESULT",
        }

        widths = {
            "run": 65,
            "id": 85,
            "title": 350,
            "attachments": 70,
            "result": 90,
        }

        for col in columns:

            self.tree.heading(
                col,
                text=headings[col],
            )

            self.tree.column(
                col,
                width=widths[col],
                anchor=W,
            )

        self.tree.pack(
            fill=BOTH,
            expand=True,
        )

        self.tree.bind(
            "<<TreeviewSelect>>",
            self.case_selected,
        )

        self.tree.bind(
            "<Double-1>",
            self.toggle_run,
        )

    # ========================================================
    # DETAILS
    # ========================================================

    def create_details(
        self,
        parent,
    ):

        frame = ttk.LabelFrame(
            parent,
            text="Test Case Details",
            padding=8,
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        self.details = ttk.Label(
            frame,
            text="Select a test case",
            font=(
                "Segoe UI",
                11,
                "bold",
            ),
            justify=LEFT,
        )

        self.details.pack(
            fill=X
        )

        action = ttk.Frame(
            frame
        )

        action.pack(
            fill=X,
            pady=8,
        )

        self.run_button = ttk.Button(
            action,
            text="RUN THIS TEST",
            command=self.toggle_current_run,
        )

        self.run_button.pack(
            side=LEFT,
            padx=2,
        )

        ttk.Label(
            action,
            text="Result:",
        ).pack(
            side=LEFT,
            padx=(15, 4),
        )

        self.case_result = ttk.Combobox(
            action,
            textvariable=self.case_status,
            values=[
                "Passed",
                "Failed",
                "Skipped",
            ],
            state="readonly",
            width=12,
        )

        self.case_result.pack(
            side=LEFT
        )

        self.case_result.bind(
            "<<ComboboxSelected>>",
            self.case_result_changed,
        )

        ttk.Entry(
            action,
            textvariable=self.comment,
        ).pack(
            side=LEFT,
            fill=X,
            expand=True,
            padx=8,
        )

        notebook = ttk.Notebook(
            frame
        )

        notebook.pack(
            fill=BOTH,
            expand=True,
        )

        steps = ttk.Frame(
            notebook
        )

        attachments = ttk.Frame(
            notebook
        )

        notebook.add(
            steps,
            text="Steps",
        )

        notebook.add(
            attachments,
            text="Attachments",
        )

        self.create_steps(
            steps
        )

        self.create_attachments(
            attachments
        )

    # ========================================================
    # STEPS
    # ========================================================

    def create_steps(
        self,
        parent,
    ):

        self.steps_tree = ttk.Treeview(
            parent,
            columns=(
                "number",
                "action",
                "expected",
            ),
            show="headings",
        )

        for col, title, width in [
            (
                "number",
                "#",
                45,
            ),
            (
                "action",
                "Action",
                300,
            ),
            (
                "expected",
                "Expected Result",
                300,
            ),
        ]:

            self.steps_tree.heading(
                col,
                text=title,
            )

            self.steps_tree.column(
                col,
                width=width,
            )

        self.steps_tree.pack(
            fill=BOTH,
            expand=True,
        )

    # ========================================================
    # ATTACHMENTS
    # ========================================================

    def create_attachments(
        self,
        parent,
    ):

        self.attach_tree = ttk.Treeview(
            parent,
            columns=(
                "scope",
                "file",
                "size",
            ),
            show="headings",
        )

        for col, title, width in [
            (
                "scope",
                "Scope",
                100,
            ),
            (
                "file",
                "File",
                300,
            ),
            (
                "size",
                "Size",
                90,
            ),
        ]:

            self.attach_tree.heading(
                col,
                text=title,
            )

            self.attach_tree.column(
                col,
                width=width,
            )

        self.attach_tree.pack(
            fill=BOTH,
            expand=True,
        )

    # ========================================================
    # EXECUTION PANEL
    # ========================================================

    def create_execution(
        self,
        parent,
    ):

        frame = ttk.LabelFrame(
            parent,
            text="Execution",
            padding=8,
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        self.summary = ttk.Label(
            frame,
            text=(
                "Fetch test cases "
                "to begin."
            ),
            justify=LEFT,
            anchor=N + W,
        )

        self.summary.pack(
            fill=X
        )

        self.progress = ttk.Progressbar(
            frame,
            mode="determinate",
        )

        self.progress.pack(
            fill=X,
            pady=10,
        )

        self.log_text = Text(
            frame,
            height=15,
            wrap="word",
        )

        self.log_text.pack(
            fill=BOTH,
            expand=True,
        )

        buttons = ttk.Frame(
            frame
        )

        buttons.pack(
            fill=X,
            pady=8,
        )

        ttk.Button(
            buttons,
            text="PREVIEW",
            command=self.preview,
        ).pack(
            side=LEFT
        )

        ttk.Button(
            buttons,
            text="EXECUTE & PUBLISH",
            command=self.execute,
        ).pack(
            side=RIGHT
        )

    # ========================================================
    # UI HELPERS
    # ========================================================

    def log(
        self,
        text,
    ):

        def update():
            self.log_text.insert(
                END,
                text + "\n",
            )

            self.log_text.see(
                END
            )

        self.root.after(
            0,
            update,
        )

    def browse(self):

        folder = filedialog.askdirectory()

        if folder:
            self.folder.set(
                folder
            )

    def set_selected(
        self,
        value,
    ):

        for tc in self.test_cases:
            tc.selected = value

        self.refresh_list()

        if self.current:
            self.show_case(
                self.current
            )

    # ========================================================
    # LIST
    # ========================================================

    def refresh_list(self):

        for item in self.tree.get_children():
            self.tree.delete(
                item
            )

        search = (
            self.search.get()
            .lower()
            .strip()
        )

        selected_filter = (
            self.filter.get()
        )

        for tc in self.test_cases:

            text = (
                f"{tc.id} "
                f"{tc.title}"
            ).lower()

            if (
                search
                and search not in text
            ):
                continue

            if (
                selected_filter == "Run"
                and not tc.selected
            ):
                continue

            if (
                selected_filter == "Don't Run"
                and tc.selected
            ):
                continue

            if selected_filter in (
                "Passed",
                "Failed",
                "Skipped",
            ):

                if tc.status != selected_filter:
                    continue

            attachments = len(
                tc.attachments
            )

            if (
                selected_filter
                == "Has Attachments"
                and attachments == 0
            ):
                continue

            self.tree.insert(
                "",
                END,
                iid=tc.id,
                values=(
                    (
                        "YES"
                        if tc.selected
                        else "NO"
                    ),
                    tc.id,
                    tc.title,
                    attachments,
                    tc.status,
                ),
            )

        self.update_summary()

    def update_summary(self):

        selected = [
            tc
            for tc in self.test_cases
            if tc.selected
        ]

        passed = sum(
            tc.status == "Passed"
            for tc in selected
        )

        failed = sum(
            tc.status == "Failed"
            for tc in selected
        )

        skipped = sum(
            tc.status == "Skipped"
            for tc in selected
        )

        self.summary.configure(
            text=(
                f"Project: "
                f"{self.project.get()}\n"
                f"Plan: "
                f"{self.plan.get()}\n"
                f"Suite: "
                f"{self.suite.get()}\n"
                f"Build: "
                f"{self.build.get()}\n\n"
                f"TOTAL TEST CASES: "
                f"{len(self.test_cases)}\n"
                f"RUNNING: "
                f"{len(selected)}\n"
                f"NOT RUNNING: "
                f"{len(self.test_cases) - len(selected)}\n\n"
                f"PASSED: {passed}\n"
                f"FAILED: {failed}\n"
                f"SKIPPED: {skipped}"
            )
        )

    # ========================================================
    # FETCH
    # ========================================================

    def fetch(self):

        if not all([
            self.org.get().strip(),
            self.project.get().strip(),
            self.pat.get().strip(),
        ]):

            messagebox.showerror(
                "Error",
                (
                    "Organization, Project "
                    "and PAT are required."
                ),
            )

            return

        try:

            plan = int(
                self.plan.get()
            )

            suite = int(
                self.suite.get()
            )

        except ValueError:

            messagebox.showerror(
                "Error",
                (
                    "Plan ID and Suite ID "
                    "must be numeric."
                ),
            )

            return

        self.status.set(
            "Fetching..."
        )

        self.log_text.delete(
            "1.0",
            END,
        )

        threading.Thread(
            target=self.fetch_worker,
            args=(
                plan,
                suite,
            ),
            daemon=True,
        ).start()

    def fetch_worker(
        self,
        plan,
        suite,
    ):

        try:

            self.client = ADOClient(
                self.org.get(),
                self.project.get(),
                self.pat.get(),
                self.debug.get(),
            )

            self.log(
                "Getting test suite..."
            )

            suite_data = (
                self.client.get_suite(
                    plan,
                    suite,
                )
            )

            self.log(
                f"Suite: "
                f"{suite_data.get('name', suite)}"
            )

            self.log(
                "Getting test points..."
            )

            points = (
                self.client.get_points(
                    plan,
                    suite,
                )
            )

            self.log(
                f"Test points found: "
                f"{len(points)}"
            )

            cases = []

            for index, point in enumerate(
                points,
                1,
            ):

                tc = TestCase(
                    point
                )

                if not tc.id:
                    continue

                self.log(
                    f"Loading "
                    f"{index}/{len(points)} "
                    f"TC {tc.id}"
                )

                try:

                    tc.steps = (
                        self.client.get_steps(
                            tc.id
                        )
                    )

                except Exception as exc:

                    self.log(
                        f"Steps warning "
                        f"{tc.id}: {exc}"
                    )

                cases.append(
                    tc
                )

            (
                mapping,
                unknown,
                unsupported,
            ) = build_attachment_map(
                cases,
                self.folder.get(),
            )

            for tc in cases:

                tc.attachments = (
                    mapping.get(
                        tc.id,
                        [],
                    )
                )

            self.test_cases = cases

            self.by_id = {
                tc.id: tc
                for tc in cases
            }

            self.root.after(
                0,
                self.refresh_list,
            )

            self.log(
                f"Loaded "
                f"{len(cases)} "
                f"test cases."
            )

            if unknown:

                self.log(
                    f"Unknown attachments: "
                    f"{len(unknown)}"
                )

            if unsupported:

                self.log(
                    f"Unsupported attachments: "
                    f"{len(unsupported)}"
                )

            self.root.after(
                0,
                lambda: self.status.set(
                    "Ready"
                ),
            )

        except Exception as exc:

            self.log(
                f"ERROR: {exc}"
            )

            self.root.after(
                0,
                lambda e=exc:
                    messagebox.showerror(
                        "Fetch Error",
                        str(e),
                    ),
            )

            self.root.after(
                0,
                lambda: self.status.set(
                    "Error"
                ),
            )

    # ========================================================
    # CASE SELECTION
    # ========================================================

    def case_selected(
        self,
        event=None,
    ):

        selected = (
            self.tree.selection()
        )

        if not selected:
            return

        tc = self.by_id.get(
            selected[0]
        )

        if tc:

            self.current = tc

            self.show_case(
                tc
            )

    def toggle_run(
        self,
        event=None,
    ):

        item = self.tree.identify_row(
            event.y
        )

        if not item:
            return

        tc = self.by_id.get(
            item
        )

        if not tc:
            return

        tc.selected = (
            not tc.selected
        )

        self.refresh_list()

        self.tree.selection_set(
            tc.id
        )

        self.show_case(
            tc
        )

    def toggle_current_run(self):

        if not self.current:
            return

        self.current.selected = (
            not self.current.selected
        )

        self.refresh_list()

        self.tree.selection_set(
            self.current.id
        )

        self.show_case(
            self.current
        )

    # ========================================================
    # SHOW TEST CASE
    # ========================================================

    def show_case(
        self,
        tc,
    ):

        self.details.configure(
            text=(
                f"TC {tc.id} | "
                f"{tc.title}\n"
                f"Configuration: "
                f"{tc.configuration or 'Default'}"
            )
        )

        self.comment.set(
            tc.comment
        )

        self.case_status.set(
            tc.status
        )

        self.run_button.configure(
            text=(
                "✓ RUN THIS TEST"
                if tc.selected
                else "✕ DON'T RUN THIS TEST"
            )
        )

        for item in (
            self.steps_tree
            .get_children()
        ):

            self.steps_tree.delete(
                item
            )

        for step in tc.steps:

            self.steps_tree.insert(
                "",
                END,
                values=(
                    step["number"],
                    step["action"],
                    step["expected"],
                ),
            )

        self.refresh_attachments(
            tc
        )

    # ========================================================
    # TEST CASE RESULT
    # ========================================================

    def case_result_changed(
        self,
        event=None,
    ):

        if not self.current:
            return

        tc = self.current

        tc.status = (
            self.case_status.get()
        )

        tc.comment = (
            self.comment.get()
        )

        self.refresh_list()

    # ========================================================
    # ATTACHMENTS
    # ========================================================

    def refresh_attachments(
        self,
        tc,
    ):

        for item in (
            self.attach_tree
            .get_children()
        ):

            self.attach_tree.delete(
                item
            )

        for attachment in tc.attachments:

            self.attach_tree.insert(
                "",
                END,
                values=(
                    "Test Case",
                    attachment[
                        "file_name"
                    ],
                    (
                        f"{attachment['size'] / 1024:.1f} "
                        "KB"
                    ),
                ),
            )

    # ========================================================
    # PREVIEW
    # ========================================================

    def preview(self):

        selected = [
            tc
            for tc in self.test_cases
            if tc.selected
        ]

        if not selected:

            messagebox.showwarning(
                "Nothing Selected",
                (
                    "Set at least one "
                    "test case to RUN."
                ),
            )

            return

        lines = [
            "EXECUTION PREVIEW",
            "=" * 70,
        ]

        for tc in selected:

            lines.append(
                f"TC {tc.id} - "
                f"{tc.title}"
            )

            lines.append(
                f"Result: "
                f"{tc.status}"
            )

            lines.append(
                f"Azure Outcome: "
                f"{ado_outcome(tc.status)}"
            )

            if tc.comment:

                lines.append(
                    f"Comment: "
                    f"{tc.comment}"
                )

            if tc.attachments:

                lines.append(
                    "Attachments:"
                )

                for attachment in (
                    tc.attachments
                ):

                    lines.append(
                        f"  - "
                        f"{attachment['file_name']}"
                    )

            lines.append("")

        dialog = Toplevel(
            self.root
        )

        dialog.title(
            "Execution Preview"
        )

        dialog.geometry(
            "900x650"
        )

        text = Text(
            dialog,
            wrap="word",
        )

        text.pack(
            fill=BOTH,
            expand=True,
            padx=10,
            pady=10,
        )

        text.insert(
            "1.0",
            "\n".join(lines),
        )

        text.configure(
            state="disabled"
        )

        ttk.Button(
            dialog,
            text="CLOSE",
            command=dialog.destroy,
        ).pack(
            pady=5
        )

    # ========================================================
    # EXECUTE
    # ========================================================

    def execute(self):

        selected = [
            tc
            for tc in self.test_cases
            if tc.selected
        ]

        if not selected:

            messagebox.showwarning(
                "Nothing Selected",
                (
                    "Set at least one "
                    "test case to RUN."
                ),
            )

            return

        if not self.client:

            messagebox.showerror(
                "Not Connected",
                "Fetch test cases first.",
            )

            return

        if not messagebox.askyesno(
            "Confirm Execution",
            (
                "Create a NEW Azure DevOps "
                "Test Run?\n\n"
                f"Tests to RUN: "
                f"{len(selected)}\n\n"
                "DON'T RUN tests will NOT "
                "be included in this run.\n\n"
                "Their existing Azure DevOps "
                "state will NOT be modified."
            ),
        ):

            return

        self.status.set(
            "Executing..."
        )

        threading.Thread(
            target=self.execute_worker,
            args=(selected,),
            daemon=True,
        ).start()

    # ========================================================
    # EXECUTION WORKER
    # ========================================================

    def execute_worker(
        self,
        selected,
    ):

        try:

            self.root.after(
                0,
                lambda: self.progress.configure(
                    maximum=len(selected),
                    value=0,
                ),
            )

            # ------------------------------------------------
            # FIND BUILD
            # ------------------------------------------------

            self.log(
                "Finding build..."
            )

            build = (
                self.client.find_build(
                    self.build.get().strip()
                )
            )

            self.log(
                f"Build found: "
                f"{build['id']} - "
                f"{build.get('buildNumber')}"
            )

            # ------------------------------------------------
            # ONLY SELECTED POINTS
            # ------------------------------------------------

            point_ids = [
                tc.point_id
                for tc in selected
                if tc.point_id
            ]

            if not point_ids:
                raise RuntimeError(
                    "No valid Test Point IDs "
                    "were found."
                )

            self.log(
                "Selected Test Points:"
            )

            for tc in selected:

                self.log(
                    f"  TC {tc.id} "
                    f"-> Point {tc.point_id} "
                    f"-> {tc.status}"
                )

            # ------------------------------------------------
            # CREATE NEW RUN
            # ------------------------------------------------

            self.log(
                "Creating Azure DevOps "
                "Test Run..."
            )

            run = self.client.create_run(
                int(
                    self.plan.get()
                ),
                point_ids,
                build,
            )

            self.run_id = int(
                run["id"]
            )

            self.log(
                f"Test Run created: "
                f"{self.run_id}"
            )

            # ------------------------------------------------
            # WAIT FOR RESULTS
            # ------------------------------------------------

            self.log(
                "Waiting for "
                "test results..."
            )

            results = (
                self.client.wait_results(
                    self.run_id,
                    point_ids,
                )
            )

            self.result_by_point = (
                result_map(
                    results
                )
            )

            self.log(
                f"Results received: "
                f"{len(results)}"
            )

            # ------------------------------------------------
            # IMPORTANT:
            # We ONLY publish selected tests.
            # ------------------------------------------------

            self.publish_results(
                selected
            )

            # ------------------------------------------------
            # ATTACHMENTS
            # ------------------------------------------------

            self.upload_attachments(
                selected
            )

            # ------------------------------------------------
            # COMPLETE RUN
            # ------------------------------------------------

            self.log(
                "Completing test run..."
            )

            self.client.complete_run(
                self.run_id
            )

            self.log(
                "Test Run completed."
            )

            # ------------------------------------------------
            # FINAL VERIFICATION
            # ------------------------------------------------

            self.verify_final_results(
                selected
            )

            self.root.after(
                0,
                lambda: self.status.set(
                    "Completed"
                ),
            )

            self.root.after(
                0,
                self.final_dialog,
            )

        except Exception as exc:

            self.log(
                f"ERROR: {exc}"
            )

            self.root.after(
                0,
                lambda e=exc:
                    messagebox.showerror(
                        "Execution Error",
                        str(e),
                    ),
            )

            self.root.after(
                0,
                lambda: self.status.set(
                    "Error"
                ),
            )

    # ========================================================
    # ACTION RESULTS
    # ========================================================

    def action_results(
        self,
        tc,
    ):
        """
        Publish each manual step with the same
        outcome selected for the test case.

        Passed  -> Passed
        Failed  -> Failed
        Skipped -> NotExecuted
        """

        outcome = ado_outcome(
            tc.status
        )

        now = utc_now()

        action_results = []

        for step in tc.steps:

            action_results.append({
                "actionPath": step[
                    "action_path"
                ],

                "iterationId": 1,

                "stepIdentifier": step[
                    "identifier"
                ],

                "outcome": outcome,

                "comment": (
                    tc.comment
                    or (
                        f"Marked "
                        f"{tc.status} by QA."
                    )
                ),

                "startedDate": now,

                "completedDate": now,

                "durationInMs": 0,
            })

        return action_results

    # ========================================================
    # ITERATION DETAILS
    # ========================================================

    def iteration_details(
        self,
        tc,
    ):
        """
        Manual test iteration containing all
        step/action results.
        """

        outcome = ado_outcome(
            tc.status
        )

        now = utc_now()

        return [{
            "id": 1,

            "iterationId": 1,

            "outcome": outcome,

            "comment": (
                tc.comment
                or (
                    f"Marked "
                    f"{tc.status} by QA."
                )
            ),

            "startedDate": now,

            "completedDate": now,

            "durationInMs": 0,

            "actionResults":
                self.action_results(
                    tc
                ),
        }]

    # ========================================================
    # PUBLISH RESULTS
    # ========================================================

    def publish_results(
        self,
        selected,
    ):
        """
        THIS IS THE IMPORTANT PART.

        Only results belonging to selected test
        points are updated.

        Unselected test cases are completely
        ignored.
        """

        updates = []

        for number, tc in enumerate(
            selected,
            1,
        ):

            point_key = str(
                tc.point_id
            )

            result = (
                self.result_by_point.get(
                    point_key
                )
            )

            if not result:

                self.log(
                    f"Result missing "
                    f"for TC {tc.id} "
                    f"/ Point {tc.point_id}"
                )

                continue

            result_id = result.get(
                "id"
            )

            if not result_id:
                continue

            azure_outcome = (
                ado_outcome(
                    tc.status
                )
            )

            comment = (
                tc.comment
                or (
                    f"Marked "
                    f"{tc.status} by QA."
                )
            )

            update = {
                "id": result_id,

                # ------------------------------------------------
                # THIS is the TEST CASE OUTCOME.
                # ------------------------------------------------
                "outcome": azure_outcome,

                # ------------------------------------------------
                # Result is complete.
                # ------------------------------------------------
                "state": "Completed",

                "comment": comment,

                # ------------------------------------------------
                # Manual test steps.
                # ------------------------------------------------
                "iterationDetails":
                    self.iteration_details(
                        tc
                    ),

                # Also keep actionResults directly on
                # the result payload.
                "actionResults":
                    self.action_results(
                        tc
                    ),
            }

            updates.append(
                update
            )

            self.log(
                f"Preparing "
                f"{number}/{len(selected)} "
                f"TC {tc.id}: "
                f"{tc.status} "
                f"-> Azure outcome "
                f"{azure_outcome}"
            )

            self.root.after(
                0,
                lambda n=number:
                    self.progress.configure(
                        value=n
                    ),
            )

        if not updates:

            raise RuntimeError(
                "No results were available "
                "to publish."
            )

        self.log(
            f"Publishing "
            f"{len(updates)} "
            f"test result(s)..."
        )

        response = (
            self.client.update_results(
                self.run_id,
                updates,
            )
        )

        # ----------------------------------------------------
        # Log Azure response
        # ----------------------------------------------------

        response_count = 0

        if isinstance(
            response,
            dict,
        ):

            response_count = len(
                response.get(
                    "value",
                    [],
                )
            )

        elif isinstance(
            response,
            list,
        ):

            response_count = len(
                response
            )

        self.log(
            f"Azure DevOps updated "
            f"{response_count} result(s)."
        )

        # ----------------------------------------------------
        # VERY IMPORTANT:
        # Refresh result data from Azure DevOps.
        #
        # This confirms the outcome was actually stored.
        # ----------------------------------------------------

        self.log(
            "Refreshing results from "
            "Azure DevOps for verification..."
        )

        refreshed_results = (
            self.client.get_results(
                self.run_id
            )
        )

        refreshed_map = result_map(
            refreshed_results
        )

        verification_errors = []

        for tc in selected:

            point_key = str(
                tc.point_id
            )

            expected = ado_outcome(
                tc.status
            )

            refreshed = (
                refreshed_map.get(
                    point_key
                )
            )

            if not refreshed:

                verification_errors.append(
                    (
                        f"TC {tc.id}: "
                        "result not found after update"
                    )
                )

                continue

            actual = refreshed.get(
                "outcome"
            )

            state = refreshed.get(
                "state"
            )

            self.log(
                f"VERIFY TC {tc.id}: "
                f"expected={expected}, "
                f"actual={actual}, "
                f"state={state}"
            )

            if actual != expected:

                verification_errors.append(
                    (
                        f"TC {tc.id}: "
                        f"expected {expected}, "
                        f"Azure returned {actual}"
                    )
                )

        if verification_errors:

            for error in verification_errors:
                self.log(
                    "OUTCOME VERIFICATION ERROR: "
                    + error
                )

            raise RuntimeError(
                "Azure DevOps did not retain "
                "one or more selected test outcomes:\n"
                + "\n".join(
                    verification_errors
                )
            )

        # Replace local result map with verified
        # Azure DevOps results.
        self.result_by_point = (
            refreshed_map
        )

        self.log(
            "All selected test outcomes "
            "verified successfully."
        )

    # ========================================================
    # FINAL RESULT VERIFICATION
    # ========================================================

    def verify_final_results(
        self,
        selected,
    ):
        """
        Final read after the run is completed.

        This gives us an additional confirmation that
        Passed / Failed / NotExecuted were retained.
        """

        self.log(
            "Performing final outcome verification..."
        )

        results = (
            self.client.get_results(
                self.run_id
            )
        )

        mapping = result_map(
            results
        )

        errors = []

        for tc in selected:

            result = mapping.get(
                str(tc.point_id)
            )

            if not result:

                errors.append(
                    f"TC {tc.id}: result missing"
                )

                continue

            expected = ado_outcome(
                tc.status
            )

            actual = result.get(
                "outcome"
            )

            state = result.get(
                "state"
            )

            self.log(
                f"FINAL TC {tc.id}: "
                f"{actual} / {state}"
            )

            if actual != expected:

                errors.append(
                    (
                        f"TC {tc.id}: "
                        f"expected {expected}, "
                        f"got {actual}"
                    )
                )

        if errors:

            raise RuntimeError(
                "Final Azure DevOps outcome "
                "verification failed:\n"
                + "\n".join(errors)
            )

        self.log(
            "Final outcome verification passed."
        )

    # ========================================================
    # ATTACHMENTS
    # ========================================================

    def upload_attachments(
        self,
        selected,
    ):
        total = 0

        uploaded = 0

        failed = 0

        for tc in selected:

            result = (
                self.result_by_point.get(
                    str(tc.point_id)
                )
            )

            if not result:
                continue

            result_id = result.get(
                "id"
            )

            if not result_id:
                continue

            for attachment in (
                tc.attachments
            ):

                total += 1

                try:

                    self.client.upload_attachment(
                        self.run_id,
                        result_id,
                        attachment[
                            "file_path"
                        ],
                    )

                    uploaded += 1

                    self.log(
                        f"Uploaded "
                        f"{attachment['file_name']} "
                        f"for TC {tc.id}"
                    )

                except Exception as exc:

                    failed += 1

                    self.log(
                        f"Attachment failed "
                        f"for TC {tc.id}: "
                        f"{exc}"
                    )

        self.log(
            f"Attachments: "
            f"{uploaded}/{total} "
            f"uploaded; "
            f"{failed} failed."
        )

    # ========================================================
    # FINAL DIALOG
    # ========================================================

    def final_dialog(
        self,
    ):

        selected = [
            tc
            for tc in self.test_cases
            if tc.selected
        ]

        passed = sum(
            tc.status == "Passed"
            for tc in selected
        )

        failed = sum(
            tc.status == "Failed"
            for tc in selected
        )

        skipped = sum(
            tc.status == "Skipped"
            for tc in selected
        )

        dialog = Toplevel(
            self.root
        )

        dialog.title(
            "Execution Complete"
        )

        dialog.geometry(
            "600x500"
        )

        frame = ttk.Frame(
            dialog,
            padding=20,
        )

        frame.pack(
            fill=BOTH,
            expand=True,
        )

        ttk.Label(
            frame,
            text="EXECUTION COMPLETE",
            font=(
                "Segoe UI",
                18,
                "bold",
            ),
        ).pack(
            pady=10
        )

        ttk.Label(
            frame,
            text=(
                f"Test Run ID: "
                f"{self.run_id}\n\n"

                f"Tests Run: "
                f"{len(selected)}\n"

                f"Passed: "
                f"{passed}\n"

                f"Failed: "
                f"{failed}\n"

                f"Skipped: "
                f"{skipped}\n\n"

                "Azure DevOps outcomes "
                "were verified after publishing."
            ),
            justify=LEFT,
            font=(
                "Segoe UI",
                11,
            ),
        ).pack(
            anchor=W,
            pady=10,
        )

        ttk.Button(
            frame,
            text="CLOSE",
            command=dialog.destroy,
        ).pack(
            pady=15
        )


# ============================================================
# MAIN
# ============================================================

def main():

    root = Tk()

    App(
        root
    )

    root.mainloop()


if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        sys.exit(1)

    except Exception as exc:

        print(
            f"Application error: {exc}"
        )

        sys.exit(1)
