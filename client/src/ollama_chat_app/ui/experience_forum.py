from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from ..services.remote_services import RemoteExperienceForumService


class ExperienceForumPanel(QWidget):
    """A quiet, readable topic wall for the cloud forum route.

    The UI deliberately keeps the interaction small: a topic filter, a post
    composer and lightweight like/comment actions. The cloud RPC can replace
    the in-memory feed without changing the member navigation contract.
    """

    data_changed = Signal()

    def __init__(self, cloud_client=None, parent=None):
        super().__init__(parent)
        self.cloud_client = cloud_client
        self.cloud_service = (
            RemoteExperienceForumService(cloud_client) if cloud_client is not None else None
        )
        self.user_id = None
        self.topic = "高血压专区"
        self._posts: list[dict] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 10)
        heading = QHBoxLayout()
        title = QLabel("经验论坛")
        title.setObjectName("PageTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索话题")
        self.search.returnPressed.connect(self._search_topic)
        heading.addWidget(self.search)
        search_button = QPushButton("搜索")
        search_button.clicked.connect(self._search_topic)
        heading.addWidget(search_button)
        root.addLayout(heading)

        recommendation = QFrame()
        recommendation.setObjectName("HealthCard")
        rec_layout = QHBoxLayout(recommendation)
        rec_layout.addWidget(QLabel("推荐话题"))
        for name in ("高血压专区", "高胆固醇专区", "高血糖专区"):
            button = QPushButton(name)
            button.clicked.connect(lambda _checked=False, value=name: self._select_topic(value))
            rec_layout.addWidget(button)
        rec_layout.addStretch(1)
        root.addWidget(recommendation)

        self.topic_label = QLabel("高血压专区")
        self.topic_label.setObjectName("SectionTitle")
        root.addWidget(self.topic_label)
        self.feed = QListWidget()
        self.feed.setAlternatingRowColors(True)
        root.addWidget(self.feed, 1)

        composer = QFrame()
        composer.setObjectName("HealthCard")
        compose_layout = QVBoxLayout(composer)
        self.editor = QTextEdit()
        self.editor.setPlaceholderText("分享一条真实经验，尽量写清时间和做法")
        self.editor.setMaximumHeight(90)
        compose_layout.addWidget(self.editor)
        send = QPushButton("发布")
        send.setObjectName("PrimaryButton")
        send.clicked.connect(self._publish)
        compose_layout.addWidget(send, 0, Qt.AlignmentFlag.AlignRight)
        root.addWidget(composer)

    def set_user(self, user_id):
        self.user_id = user_id
        self.refresh()

    def _select_topic(self, topic: str):
        self.topic = topic
        self.topic_label.setText(topic)
        self.refresh()

    def _search_topic(self):
        value = self.search.text().strip()
        if value:
            self._select_topic(value[:40])

    def _publish(self):
        text = self.editor.toPlainText().strip()
        if not text or self.user_id is None:
            return
        if self.cloud_service is not None:
            try:
                self.cloud_service.create_post(self.user_id, self.topic, text)
                self.editor.clear()
                self.refresh()
                self.data_changed.emit()
                return
            except Exception:
                # A temporary network outage should not erase what the user
                # just typed; keep the local draft visible for this session.
                pass
        self._posts.insert(0, {
            "topic": self.topic,
            "content": text,
            "author": "我",
            "likes": 0,
            "comments": 0,
            "time": datetime.now().strftime("%m-%d %H:%M"),
        })
        self.editor.clear()
        self.refresh()
        self.data_changed.emit()

    def refresh(self):
        self.feed.clear()
        posts = []
        if self.cloud_service is not None and self.user_id is not None:
            try:
                posts = self.cloud_service.list_posts(self.user_id, self.topic, 50)
            except Exception:
                posts = []
        if not posts:
            posts = [item for item in self._posts if item.get("topic") == self.topic]
        if not posts:
            posts = [{
                "topic": self.topic,
                "content": "欢迎来到这个话题，分享生活经验前请先核对自己的实际情况。",
                "author": "平台提示",
                "likes": 0,
                "comments": 0,
                "time": "刚刚",
            }]
        for post in posts:
            item = QListWidgetItem()
            widget = QFrame()
            layout = QVBoxLayout(widget)
            top = QHBoxLayout()
            top.addWidget(QLabel(str(post.get("author", "会员"))))
            top.addStretch(1)
            top.addWidget(QLabel(str(post.get("time", ""))))
            layout.addLayout(top)
            text = QLabel(str(post.get("content", "")))
            text.setWordWrap(True)
            layout.addWidget(text)
            actions = QHBoxLayout()
            like = QPushButton(f"赞 {post.get('likes', 0)}")
            like.clicked.connect(lambda _checked=False, p=post: self._like(p))
            comment = QPushButton(f"评论 {post.get('comments', 0)}")
            comment.clicked.connect(lambda: self._comment(post))
            actions.addWidget(like)
            actions.addWidget(comment)
            actions.addStretch(1)
            layout.addLayout(actions)
            item.setSizeHint(widget.sizeHint())
            self.feed.addItem(item)
            self.feed.setItemWidget(item, widget)

    def _like(self, post: dict):
        if self.cloud_service is not None and self.user_id is not None and post.get("id"):
            try:
                result = self.cloud_service.toggle_like(self.user_id, int(post["id"]))
                post.update(result)
                self.refresh()
                return
            except Exception:
                pass
        post["likes"] = int(post.get("likes", 0)) + 1
        self.refresh()

    def _comment(self, post: dict):
        if self.cloud_service is not None and self.user_id is not None and post.get("id"):
            content, ok = QInputDialog.getText(self, "发表评论", "想说点什么？")
            if ok and content.strip():
                try:
                    self.cloud_service.add_comment(self.user_id, int(post["id"]), content)
                    post["comments"] = int(post.get("comments", 0)) + 1
                    self.refresh()
                    return
                except Exception:
                    pass
        post["comments"] = int(post.get("comments", 0)) + 1
        self.refresh()
