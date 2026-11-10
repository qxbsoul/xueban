"""
Multi-Agent Content Creation Workflow Example

This example demonstrates a complex workflow with multiple specialized agents
working together to create high-quality content.
"""

import asyncio
from agentgraph import AgentGraph, Agent, AgentConfig, SQLiteMemory
from typing import Dict, Any


def create_content_creation_agents():
    """Create the agents for content creation workflow."""

    # Research Specialist
    researcher_config = AgentConfig(
        name="researcher",
        role="Research Specialist",
        prompt="""
        You are an expert researcher. Your task is to gather comprehensive information about the given topic.

        Requirements:
        1. Search for current and relevant information
        2. Identify key points and trends
        3. Gather supporting data and examples
        4. Note any controversies or different perspectives

        Output a structured report with:
        - Main overview
        - Key findings (3-5 points)
        - Supporting evidence
        - Future implications
        """,
        tools=["web-search", "document-analysis"],
        max_tokens=4000,
        temperature=0.3  # More precise for research
    )
    researcher = Agent(researcher_config, "researcher")

    # Content Analyst
    analyst_config = AgentConfig(
        name="analyst",
        role="Content Analyst",
        prompt="""
        You analyze the research findings and extract the most valuable insights for content creation.

        Analyze the research for:
        1. Key takeaways that would engage readers
        2. Unique angles or perspectives
        3. Data points that support the main message
        4. Potential counterarguments to address

        Provide a content strategy with:
        - Target audience analysis
        - Core message and tone
        - Structure outline
        - Key points to emphasize
        """,
        tools=["text-analysis", "sentiment-analysis"],
        max_tokens=3000
    )
    analyst = Agent(analyst_config, "analyst")

    # Creative Writer
    writer_config = AgentConfig(
        name="writer",
        role="Creative Writer",
        prompt="""
        You create engaging, well-structured content based on the research and analysis.

        Writing guidelines:
        1. Start with a compelling hook
        2. Maintain clear, accessible language
        3. Use examples and data to support points
        4. End with a strong conclusion or call to action
        5. Keep paragraphs short (2-4 sentences)

        Structure:
        - Introduction (hook + thesis)
        - Main body (3-5 key points)
        - Conclusion (summary + next steps)
        """,
        tools=["grammar-checker", "style-analyzer"],
        max_tokens=5000,
        temperature=0.8  # More creative
    )
    writer = Agent(writer_config, "writer")

    # Quality Reviewer
    reviewer_config = AgentConfig(
        name="reviewer",
        role="Quality Reviewer",
        prompt="""
        You review the content for quality, accuracy, and engagement.

        Check for:
        1. Factual accuracy (cross-reference with research)
        2. Grammar and spelling
        3. Readability and flow
        4. Engagement level
        5. Alignment with target audience
        6. Missing or weak points

        Provide:
        - Overall quality score (1-10)
        - Specific feedback for improvement
        - Final recommendations
        """,
        tools=["fact-checker", "readability-score"],
        max_tokens=2000
    )
    reviewer = Agent(reviewer_config, "reviewer")

    # Final Editor
    editor_config = AgentConfig(
        name="editor",
        role="Final Editor",
        prompt="""
        You take the reviewed content and make final improvements based on feedback.

        Your task:
        1. Incorporate all constructive feedback
        2. Ensure consistent tone and style
        3. Polish language for clarity and impact
        4. Verify all claims are supported
        5. Create a compelling final version

        Output the polished final content.
        """,
        tools=[],
        max_tokens=4000
    )
    editor = Agent(editor_config, "editor")

    return {
        "researcher": researcher,
        "analyst": analyst,
        "writer": writer,
        "reviewer": reviewer,
        "editor": editor
    }


async def execute_content_creation_workflow(topic: str, user_id: str):
    """
    Execute the multi-agent content creation workflow.

    Args:
        topic: The topic to create content about
        user_id: The user initiating the request
    """

    # Initialize the graph
    memory = SQLiteMemory()
    graph = AgentGraph(memory=memory)

    # Create and add agents
    agents = create_content_creation_agents()
    for agent in agents.values():
        graph.add_agent(agent)

    print(f"Starting content creation for: {topic}")
    print("=" * 50)

    # Execute the workflow
    results = []
    async for result in graph.invoke(
        input=f"Create a comprehensive article about {topic}",
        user_id=user_id,
        workflow_id="content-creation"
    ):
        results.append(result)

        # Print progress
        print(f"\nProgress: {result.progress}%")
        print(f"Current Agent: {result.current_agent}")

        if hasattr(result, 'output') and result.output:
            print(f"Output: {result.output[:200]}...")

        if hasattr(result, 'error') and result.error:
            print(f"Error: {result.error}")

    # Print final result
    print("\n" + "=" * 50)
    print("Content Creation Complete!")
    print("=" * 50)

    # Find the final output
    final_result = None
    for result in results:
        if hasattr(result, 'output') and result.output:
            final_result = result

    if final_result:
        print("\nFinal Content:")
        print("-" * 30)
        print(final_result.output)
        print("-" * 30)

        print(f"\nStatistics:")
        print(f"Total agents used: {len(set(r.current_agent for r in results if hasattr(r, 'current_agent')))}")
        print(f"Total execution time: {final_result.get('total_time', 'N/A')} seconds")
    else:
        print("No final output generated.")

    return results


async def interactive_content_creation():
    """Interactive version of content creation workflow."""

    print("🎯 Multi-Agent Content Creation System")
    print("=" * 40)

    # Get user input
    topic = input("\nWhat topic would you like content about? ")
    user_id = "demo-user"

    # Validate input
    if not topic.strip():
        print("Please enter a valid topic.")
        return

    # Execute workflow
    await execute_content_creation_workflow(topic.strip(), user_id)


def analyze_workflow_efficiency(results: list):
    """
    Analyze the efficiency of the workflow execution.

    Args:
        results: List of execution results
    """
    print("\n📊 Workflow Analysis")
    print("=" * 30)

    # Calculate statistics
    agent_steps = {}
    total_time = 0

    for result in results:
        if hasattr(result, 'current_agent'):
            agent = result.current_agent
            agent_steps[agent] = agent_steps.get(agent, 0) + 1

        if hasattr(result, 'execution_time'):
            total_time += result.execution_time

    # Display statistics
    print(f"Total steps executed: {len(results)}")
    print(f"Total execution time: {total_time:.2f} seconds")
    print(f"Unique agents used: {len(agent_steps)}")

    print("\nAgent Usage:")
    for agent, steps in agent_steps.items():
        print(f"  {agent}: {steps} steps")

    # Calculate efficiency metrics
    if total_time > 0:
        avg_step_time = total_time / len(results)
        print(f"\nAverage step time: {avg_step_time:.2f} seconds")

        # Identify bottlenecks
        slow_agents = [(agent, steps) for agent, steps in agent_steps.items()
                       if steps * avg_step_time > 30]  # More than 30 seconds total

        if slow_agents:
            print("\n⚠️ Potential bottlenecks:")
            for agent, steps in slow_agents:
                print(f"  {agent}: {steps} steps ({steps * avg_step_time:.2f}s total)")


if __name__ == "__main__":
    # Run interactive workflow
    asyncio.run(interactive_content_creation())

    # Example with predefined topic
    print("\n" + "=" * 60)
    print("Running example with predefined topic...")

    asyncio.run(
        execute_content_creation_workflow(
            "The Future of Artificial Intelligence in Healthcare",
            "example-user"
        )
    )

    # Note: In a real implementation, you would need to:
    # 1. Implement the required tools (web-search, document-analysis, etc.)
    # 2. Set up proper error handling
    # 3. Add logging and monitoring
    # 4. Configure appropriate model endpoints
    # 5. Handle authentication and authorization